"""Library sync: kick off a background sync and stream its progress over SSE."""
import asyncio
import json
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from yt_playlist.library import sync as sync_mod


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, jobs = ctx.store, ctx.now_fn, ctx.jobs

    @router.post("/sync")
    def do_sync():
        # Run the (slow, network-bound) sync in a background thread and stream progress over SSE.
        clients = ctx.client_provider()
        job = jobs.create()

        def run():
            try:
                sync_mod.sync_all(store, clients, now_fn(), on_progress=job.events.append,
                                  on_auth_expired=lambda iid, label: ctx.auth_expired.__setitem__(iid, label or str(iid)),
                                  on_auth_ok=lambda iid: ctx.auth_expired.pop(iid, None))
            except Exception as e:  # noqa: BLE001 - report any failure to the stream
                detail = str(e) or type(e).__name__
                job.error = detail
                job.events.append({"type": "err", "text": f"sync failed: {detail}"})
            finally:
                job.done = True
                if ctx.rec_worker:                  # rebuild recs off the sync path (debounced)
                    ctx.rec_worker.trigger()
                if ctx.enrich_worker:               # new tracks arrived — drain them (queue-jumped)
                    ctx.enrich_worker.trigger()

        threading.Thread(target=run, daemon=True).start()
        return JSONResponse({"job_id": job.id})

    @router.post("/sync/plays")
    def do_sync_plays():
        # Fast path: pull only new plays (history) and likes (Liked Music), skipping the heavy
        # full-library enumeration. Shares the same background-job + SSE machinery as /sync.
        clients = ctx.client_provider()
        job = jobs.create()

        def run():
            try:
                sync_mod.sync_plays_all(store, clients, now_fn(), on_progress=job.events.append,
                                        on_auth_expired=lambda iid, label: ctx.auth_expired.__setitem__(iid, label or str(iid)),
                                        on_auth_ok=lambda iid: ctx.auth_expired.pop(iid, None))
            except Exception as e:  # noqa: BLE001 - report any failure to the stream
                detail = str(e) or type(e).__name__
                job.error = detail
                job.events.append({"type": "err", "text": f"sync failed: {detail}"})
            finally:
                job.done = True
                if ctx.rec_worker:                  # new plays/likes feed the taste model — rebuild (debounced)
                    ctx.rec_worker.trigger()

        threading.Thread(target=run, daemon=True).start()
        return JSONResponse({"job_id": job.id})

    @router.post("/sync/auto")
    async def set_auto_sync(request: Request):
        # Toggle background auto-sync of plays. Persisted as a setting; the RecWorker's auto-sync
        # daemon re-reads it each tick (~30 min) and pulls new plays/likes while it's on.
        enabled = (await request.form()).get("enabled") == "1"
        store.set_setting("auto_sync_plays", "1" if enabled else "0")
        return JSONResponse({"enabled": enabled})

    @router.get("/sync/events/{job_id}")
    async def sync_events(request: Request, job_id: int):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such sync job")

        async def gen():
            sent = 0
            while True:
                while sent < len(job.events):
                    yield f"data: {json.dumps(job.events[sent])}\n\n"
                    sent += 1
                if job.done:
                    yield f"data: {json.dumps({'type': 'end', 'error': job.error})}\n\n"
                    return
                if await request.is_disconnected():   # browser navigated away — stop streaming
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return router
