"""Tools › Enrichment: corpus coverage charts + worker state/pause, all served from the store's
enrichment stats. The page polls /enrich/stats so the bars advance live as the worker drains."""
import asyncio
import json
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from yt_playlist.providers import enrichment, waterfall
from yt_playlist.web import viz


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _ctx():
        cov = store.coverage_stats()
        total = cov["total"]
        def pct(k):
            return round(100 * cov[k] / total) if total else 0
        remaining = store.queue_remaining()
        enabled = store.get_setting("enrich_worker_enabled", "1") == "1"
        busy = bool(ctx.enrich_worker and ctx.enrich_worker.busy)
        if not enabled:
            state = "paused"
        elif busy or remaining > 0:
            state = "running"
        else:
            state = "idle"
        return {
            "cov": cov,
            "pct": {k: pct(k) for k in
                    ("processed", "genre", "year", "bpm", "energy", "danceability")},
            "remaining": remaining, "conflicts": store.outstanding_conflicts(),
            "enabled": enabled, "state": state,
            "spark": viz.area_spark(store.processed_timeline()),
        }

    @router.get("/enrich")
    def enrich_page(request: Request):
        return templates.TemplateResponse(request, "enrich.html", _ctx())

    @router.get("/enrich/stats")
    def enrich_stats(request: Request):
        return templates.TemplateResponse(request, "_partials/enrich_stats.html", _ctx())

    @router.post("/enrich/toggle")
    def enrich_toggle(request: Request):
        was_on = store.get_setting("enrich_worker_enabled", "1") == "1"
        store.set_setting("enrich_worker_enabled", "0" if was_on else "1")
        if was_on is False and ctx.enrich_worker:     # just turned ON -> wake the drain loop
            ctx.enrich_worker.trigger()
        return templates.TemplateResponse(request, "_partials/enrich_stats.html", _ctx())

    def _genre_candidates(request, track_id):
        track = store.genre_provenance(track_id)
        if track is None:
            return Response(status_code=404)
        return templates.TemplateResponse(request, "_partials/genre_candidates.html", {"track": track})

    @router.get("/track/{track_id}/genre-candidates")
    def genre_candidates(request: Request, track_id: int):
        return _genre_candidates(request, track_id)

    @router.post("/track/{track_id}/genre-lookup")
    def genre_lookup(track_id: int):
        """Start the normal provider waterfall for one track; progress streams over SSE."""
        track = store.track_for_waterfall(track_id)
        if track is None:
            return Response(status_code=404)
        job = ctx.jobs.create()
        job.source = "genre-dialog"

        def run():
            try:
                waterfall.run_waterfall(store, [track], enrichment.load_config(store), job.events.append)
            except Exception as exc:  # noqa: BLE001
                job.error = str(exc) or type(exc).__name__
                job.events.append({"type": "err", "text": f"Lookup failed: {job.error}"})
            finally:
                job.done = True

        threading.Thread(target=run, daemon=True).start()
        return JSONResponse({"job_id": job.id})

    @router.get("/track/genre-lookup/events/{job_id}")
    async def genre_lookup_events(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None or job.source != "genre-dialog":
            raise HTTPException(status_code=404, detail="no such genre lookup")

        async def events():
            sent = 0
            while True:
                while sent < len(job.events):
                    yield f"data: {json.dumps(job.events[sent])}\n\n"
                    sent += 1
                if job.done:
                    yield f"data: {json.dumps({'type': 'end', 'error': job.error})}\n\n"
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(.1)

        return StreamingResponse(events(), media_type="text/event-stream")

    @router.post("/track/{track_id}/genre-candidates")
    async def choose_genre_candidate(request: Request, track_id: int):
        track = store.genre_provenance(track_id)
        if track is None:
            return Response(status_code=404)
        value = ((await request.form()).get("genre") or "").strip()
        # Provider candidates and custom user labels deliberately share one write path.
        if not value or len(value) > 80:
            return Response(status_code=400)
        store.set_track_genre(track_id, value)
        # The same track can occur on several visible rows. A reload updates all of them and their
        # sort data consistently; candidate choice is rare enough that a partial multi-row swap is
        # needless complexity.
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    return router
