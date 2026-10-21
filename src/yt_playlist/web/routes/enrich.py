"""Tools › Enrichment: corpus coverage charts + worker state/pause, all served from the store's
enrichment stats. The page polls /enrich/stats so the bars advance live as the worker drains."""
import asyncio
import json
import math
from yt_playlist.core.activity import activity_for

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from yt_playlist.providers import enrichment, genres as genre_lib, waterfall
from yt_playlist.web import viz
from yt_playlist.web.routes.genres import genre_list_context


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    provider_labels = {p["name"]: p["label"] for p in enrichment.PROVIDERS}

    def age(timestamp):
        if timestamp is None:
            return "never"
        seconds = max(0, int(ctx.now_fn() - timestamp))
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    def _ctx():
        cov = store.coverage_stats(include_discovery=True)
        total = cov["total"]
        def pct(k):
            if k == "processed" and total:
                # Never claim 100% while a few tracks still await their first pass.
                return math.floor(1000 * cov[k] / total) / 10
            return round(100 * cov[k] / total) if total else 0
        remaining = store.queue_remaining()
        now = ctx.now_fn()
        progress = store.enrichment_activity_stats(now)
        enabled = store.get_setting("enrich_worker_enabled", "1") == "1"
        busy = bool(ctx.enrich_worker and ctx.enrich_worker.busy)
        activity = ctx.enrich_worker.activity if ctx.enrich_worker else {}
        retry = store.enrichment_retry_status()
        art_retry = store.enrichment.artwork_retry_status()
        if not enabled:
            state = "paused"
        elif activity.get("state") in ("blocked", "error"):
            state = activity["state"]
        elif busy and activity.get("state") == "waiting":
            state = "waiting"
        elif busy:
            state = "running"
        elif retry["count"]:
            state = "retry"
        elif remaining:
            state = "queued"
        else:
            state = "idle"
        return {
            "cov": cov,
            "pct": {k: pct(k) for k in
                    ("processed", "genre", "year", "bpm", "energy", "danceability")},
            "remaining": remaining, "conflicts": store.outstanding_conflicts(),
            "enabled": enabled, "state": state,
            "activity": activity, "activity_age": age(activity.get("updated_at")),
            "retry": retry, "retry_seconds": max(0, math.ceil((retry["retry_at"] or now) - now)),
            "art_retry": art_retry,
            "art_retry_seconds": max(0, math.ceil((art_retry["retry_at"] or now) - now)),
            "progress": progress, "age": age,
            "spark": viz.area_spark(store.processed_timeline()),
        }

    @router.get("/enrich")
    def enrich_page(request: Request):
        return templates.TemplateResponse(request, "enrich.html", {
            **_ctx(),
            **genre_list_context(store),
            "builtin_count": len(genre_lib.builtin_names()),
        })

    @router.get("/enrich/stats")
    def enrich_stats(request: Request):
        return templates.TemplateResponse(request, "_partials/enrich_stats.html", _ctx())

    def detail(request, view, page):
        total = (store.queue_remaining() if view == "queue" else
                 store.coverage_stats(include_discovery=True)["processed"] if view == "processed" else
                 store.outstanding_conflicts())
        size = 20 if view == "conflicts" else 40
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        offset = (page - 1) * size
        fetch = {"queue": store.next_enrich_batch, "processed": store.enriched_tracks,
                 "conflicts": store.unresolved_enrichment_conflicts}[view]
        return templates.TemplateResponse(request, "enrich_detail.html", {
            "view": view, "rows": fetch(size, offset), "total": total, "page": page,
            "pages": pages, "start": offset + 1 if total else 0,
            "end": min(offset + size, total), "age": age,
            "provider_labels": provider_labels,
        })

    @router.get("/enrich/queue")
    def enrich_queue(request: Request, page: int = Query(1, ge=1)):
        return detail(request, "queue", page)

    @router.get("/enrich/processed")
    def enrich_processed(request: Request, page: int = Query(1, ge=1)):
        return detail(request, "processed", page)

    @router.get("/enrich/conflicts")
    def enrich_conflicts(request: Request, page: int = Query(1, ge=1)):
        return detail(request, "conflicts", page)

    @router.post("/enrich/conflicts/{track_id}/{field}")
    async def resolve_enrich_conflict(request: Request, track_id: int, field: str,
                                     page: int = Query(1, ge=1)):
        value = (await request.form()).get("value")
        if not isinstance(value, str) or not store.resolve_enrichment_candidate(
                track_id, field, value, now=ctx.now_fn()):
            raise HTTPException(status_code=400, detail="Choose a current provider candidate.")
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(request, "_partials/enrich_conflict.html", {
                "c": store.enrichment_conflict_detail(track_id, field), "saved": True,
                "provider_labels": provider_labels, "page": page,
            })
        return RedirectResponse(f"/enrich/conflicts?page={page}", status_code=303)

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

        activity_for(ctx).start_thread(run)
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
        ctx.rec_worker.trigger_mode_refresh()
        # The same track can occur on several visible rows. A reload updates all of them and their
        # sort data consistently; candidate choice is rare enough that a partial multi-row swap is
        # needless complexity.
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    return router
