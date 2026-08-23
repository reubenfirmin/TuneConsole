"""Tools › Enrichment: corpus coverage charts + worker state/pause, all served from the store's
enrichment stats. The page polls /enrich/stats so the bars advance live as the worker drains."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

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

    @router.post("/track/{track_id}/genre-candidates")
    async def choose_genre_candidate(request: Request, track_id: int):
        track = store.genre_provenance(track_id)
        if track is None:
            return Response(status_code=404)
        value = ((await request.form()).get("genre") or "").strip()
        allowed = {c["value"] for c in track["candidates"] if c["value"]}
        if value not in allowed:
            return Response(status_code=400)
        store.set_track_genre(track_id, value)
        # The same track can occur on several visible rows. A reload updates all of them and their
        # sort data consistently; candidate choice is rare enough that a partial multi-row swap is
        # needless complexity.
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    return router
