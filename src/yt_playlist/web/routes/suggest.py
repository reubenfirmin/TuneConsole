"""Recommendation-serving endpoints, returned as lazy htmx fragments."""
from fastapi import APIRouter, HTTPException, Request

from yt_playlist import recommend


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

    return router
