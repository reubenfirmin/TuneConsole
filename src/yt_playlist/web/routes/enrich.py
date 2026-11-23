"""Tools › Enrichment: corpus coverage charts + worker state/pause, all served from the store's
enrichment stats. The page polls /enrich/stats so the bars advance live as the worker drains."""
import math

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from yt_playlist.providers import enrichment, genres as genre_lib
from yt_playlist.web import viz
from yt_playlist.web.routes.genres import genre_list_context
from yt_playlist.controllers.enrichment import EnrichmentCommands
from yt_playlist.core.activity import activity_for


def _genre_progress(event):
    """Project provider progress without forwarding diagnostic text or fields."""
    kind = event.get('type')
    kind = kind if kind in {'track', 'activity', 'done', 'err'} else 'info'
    warning = bool(event.get('warning') or event.get('deferred'))
    text = ('Genre lookup did not finish. Your changes are kept.' if kind == 'err' else
            'Some suggestions are not available yet. Checking the rest…' if warning else
            'Genre lookup finished.' if kind == 'done' else 'Looking up genres…')
    result = {'type': kind, 'warning': warning, 'text': text}
    if kind == 'track':
        for key in ('i', 'n'):
            value = event.get(key)
            result[key] = value if type(value) is int and value >= 0 else 0
    return result


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    commands = EnrichmentCommands(store.enrichment_reports, store.enrichment_queue, store.enrichment_state,
        store.enrichment, store.tracks, store.settings, now=ctx.now_fn, worker=ctx.enrich_worker,
        refresh=lambda **kwargs: ctx.rec_worker.trigger_content_update(**kwargs) if ctx.rec_worker else None,
        jobs=ctx.jobs, logger=ctx.logger,
        start_thread=activity_for(ctx).start_thread)
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
        facts = commands.query()
        cov = facts['cov']
        total = cov["total"]
        def pct(k):
            if k == "processed" and total:
                # Never claim 100% while a few tracks still await their first pass.
                return math.floor(1000 * cov[k] / total) / 10
            return round(100 * cov[k] / total) if total else 0
        queue = facts['queue']
        remaining = queue["total"]
        now = ctx.now_fn()
        progress, enabled = facts['progress'], facts['enabled']
        activity, retry, art_retry, state = (facts[key] for key in ('activity', 'retry', 'art_retry', 'state'))
        timeline = facts['timeline']
        spark = viz.area_spark(timeline)
        refresh_spark = viz.area_spark([
            {"t": point["t"], "n": point["refresh"]} for point in timeline],
            maximum=max((point["n"] for point in timeline), default=0), filled=False)
        for band, point in zip(spark["bands"], timeline):
            band["refresh"] = point["refresh"]
        return {
            "cov": cov,
            "pct": {k: pct(k) for k in
                    ("processed", "genre", "year", "bpm", "danceability")},
            "remaining": remaining, "queue": queue,
            "conflicts": facts['conflicts'],
            "enabled": enabled, "state": state,
            "activity": activity, "activity_age": age(activity.get("updated_at")),
            "retry": retry, "retry_seconds": max(0, math.ceil((retry["retry_at"] or now) - now)),
            "art_retry": art_retry,
            "art_retry_seconds": max(0, math.ceil((art_retry["retry_at"] or now) - now)),
            "progress": progress, "age": age,
            "spark": spark, "refresh_spark": refresh_spark,
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
        return templates.TemplateResponse(request, "enrich_detail.html", {
            **commands.detail(view, page), "age": age,
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
        try:
            conflict = commands.resolve_conflict(track_id, field, value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(request, "_partials/enrich_conflict.html", {
                "c": conflict, "saved": True,
                "provider_labels": provider_labels, "page": page,
            })
        return RedirectResponse(f"/enrich/conflicts?page={page}", status_code=303)

    @router.post("/enrich/toggle")
    def enrich_toggle(request: Request):
        commands.toggle()
        return templates.TemplateResponse(request, "_partials/enrich_stats.html", _ctx())

    @router.post("/track/{track_id}/genre-lookup")
    def genre_lookup(track_id: int):
        """Start the normal provider waterfall for one track; progress streams over SSE."""
        job = commands.lookup_genre(store, track_id)
        if job is None:
            return Response(status_code=404)
        return JSONResponse({"job_id": job.id})

    @router.get("/track/genre-lookup/events/{job_id}")
    async def genre_lookup_events(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None or job.source != "genre-lookup":
            raise HTTPException(status_code=404, detail="Genre lookup is missing or expired.")

        from yt_playlist.web.job_events import stream_job
        return StreamingResponse(stream_job(job, request, transform=_genre_progress,
            terminal=lambda: {'type': 'end', 'error': bool(job.error)}), media_type="text/event-stream")

    return router
