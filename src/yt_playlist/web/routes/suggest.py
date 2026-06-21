"""Recommendation-serving endpoints, returned as lazy htmx fragments."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from yt_playlist import embed, recommend

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

    @router.post("/recs/rebuild")
    def recs_rebuild():
        """Rebuild the taste-embedding model from the current library (also runs after each sync)."""
        return JSONResponse({"ok": True, "count": embed.build_and_store(store)})

    @router.post("/recs/feedback")
    async def recs_feedback(request: Request):
        """Persist a feedback event. htmx removes the card by swapping in the empty response."""
        form = await request.form()
        item = form.get("item")
        if not item:
            return HTMLResponse("", status_code=422)
        kind = form.get("kind", "dismiss")
        now = now_fn()
        until = now + _SNOOZE_DAYS * 86400 if kind == "not_now" else None
        store.record_feedback(form.get("surface", "for_you"), item, kind,
                              reason=form.get("reason"), scope=form.get("scope", ""),
                              until=until, now=now)
        return HTMLResponse("")

    return router
