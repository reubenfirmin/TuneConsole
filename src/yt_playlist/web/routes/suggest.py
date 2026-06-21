"""Recommendation-serving endpoints, returned as lazy htmx fragments."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from yt_playlist import embed, recommend


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/playlist/{pid}/suggestions")
    def playlist_suggestions(request: Request, pid: int):
        if store.get_playlist(pid) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        return templates.TemplateResponse(request, "_partials/playlist_suggestions.html", {
            "suggestions": recommend.complete_playlist(store, pid),
            "pid": pid,
        })

    @router.post("/recs/rebuild")
    def recs_rebuild():
        """Rebuild the taste-embedding model from the current library (also runs after each sync)."""
        return JSONResponse({"ok": True, "count": embed.build_and_store(store)})

    return router
