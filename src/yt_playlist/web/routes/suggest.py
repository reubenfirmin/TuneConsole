"""Recommendation-serving endpoints, returned as lazy htmx fragments."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from yt_playlist import embed, recommend
from yt_playlist.rec_dao import RecDao

# feedback kinds that suppress an item (vs 'more'/'less' which only nudge future weights)
_SNOOZE_DAYS = 14


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/playlist/{pid}/suggestions")
    def playlist_suggestions(request: Request, pid: int):
        if store.get_playlist(pid) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        return templates.TemplateResponse(request, "_partials/playlist_suggestions.html", {
            "suggestions": recommend.complete_playlist(store, pid, now=now_fn()),
            "pid": pid,
        })

    @router.get("/track/{vid}/similar")
    def track_similar(request: Request, vid: str):
        """'Songs like this' — embedding neighbours of one track, rendered as a modal fragment."""
        dao = RecDao(store)
        key = dao.key_for_video(vid)
        nbrs = embed.neighbors(store, key, topn=12) if key else []
        meta = store.tracks_by_keys([k for k, _ in nbrs] + ([key] if key else []))
        items = [meta[k] for k, _ in nbrs if k in meta]
        return templates.TemplateResponse(request, "_partials/similar_modal.html",
                                          {"items": items, "seed": meta.get(key, {})})

    @router.post("/recs/rebuild")
    def recs_rebuild():
        """Rebuild the model + materialize proposals (also runs in the worker after each sync)."""
        if ctx.rec_worker:
            ctx.rec_worker.rebuild()
        else:
            embed.build_and_store(store)
        return JSONResponse({"ok": True, "count": store.rec_vectors_count()})

    @router.post("/recs/feedback")
    async def recs_feedback(request: Request):
        """Persist a feedback event. htmx removes the card by swapping in the empty response."""
        form = await request.form()
        item = form.get("item")
        if not item:
            return HTMLResponse("", status_code=422)
        kind, reason = form.get("kind", "dismiss"), form.get("reason")
        now = now_fn()
        until = now + _SNOOZE_DAYS * 86400 if kind == "not_now" else None
        store.record_feedback(form.get("surface", "for_you"), item, kind,
                              reason=reason, scope=form.get("scope", ""), until=until, now=now)
        # online weight update — but 'already know it' (own_it) suppresses WITHOUT a taste penalty
        lane = form.get("lane")
        if lane and reason != "own_it":
            if kind in ("dismiss", "less", "not_now"):
                store.nudge_weight(f"lane:{lane}", 0.85)
            elif kind == "more":
                store.nudge_weight(f"lane:{lane}", 1.15)
        return HTMLResponse("")

    return router
