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
                with ctx.sync_lock:               # never overlap the background sync daemon
                    sync_mod.sync_all(store, clients, now_fn(), on_progress=job.events.append,
                                      on_auth_expired=ctx.flag_auth_expired,
                                      on_auth_ok=ctx.clear_auth_expired)
            except Exception as e:  # noqa: BLE001 - report any failure to the stream
                detail = str(e) or type(e).__name__
                job.error = detail
                job.events.append({"type": "err", "text": f"sync failed: {detail}"})
            finally:
                job.done = True
                if ctx.rec_worker:                  # rebuild recs off the sync path (debounced)
                    ctx.rec_worker.trigger()
                if ctx.enrich_worker:               # new tracks arrived. Drain them (queue-jumped)
                    ctx.enrich_worker.trigger()

        threading.Thread(target=run, daemon=True).start()
        return JSONResponse({"job_id": job.id})

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
                if await request.is_disconnected():   # browser navigated away. Stop streaming
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return router
