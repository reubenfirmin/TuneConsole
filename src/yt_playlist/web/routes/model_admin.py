"""Read-only architecture documentation; opening it never fits or refreshes models."""
from fastapi import APIRouter, Request

from yt_playlist.controllers.model_admin import graph


def build(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/model-admin")
    def model_admin(request: Request):
        return ctx.templates.TemplateResponse(request, "model_admin.html", {"graph": graph()})

    return router
