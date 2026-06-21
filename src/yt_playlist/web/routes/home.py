"""Home tab: the default landing page — Sync control, Take-Action triage, and For-You recs."""
from fastapi import APIRouter, Request

from yt_playlist import recommend


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        return templates.TemplateResponse(request, "home.html", {
            "actions": recommend.take_action(store, now, ctx.auth_expired),
            "for_you": recommend.for_you(store, now),
            "flash": request.query_params.get("flash"),
        })

    return router
