"""Read-only architecture documentation; opening it never fits or refreshes models."""
from fastapi import APIRouter, Request

from yt_playlist.controllers.model_admin import graph
from yt_playlist.core import schema_diagram, updatecheck
from yt_playlist.core.activity import work_budget
from yt_playlist.core.db_reads import READ_SECONDS, read_sql_snapshot


def build(ctx) -> APIRouter:
    router = APIRouter()
    available = updatecheck.is_dev_install()
    ctx.templates.env.globals['model_map_available'] = available
    if not available:
        return router

    @router.get("/model-admin")
    def model_admin(request: Request):
        return ctx.templates.TemplateResponse(request, "model_admin.html", {"graph": graph()})

    @router.get("/schema-admin")
    def schema_admin(request: Request):
        with work_budget(READ_SECONDS), read_sql_snapshot(ctx.store) as conn:
            diagram = schema_diagram.diagram(conn)
        return ctx.templates.TemplateResponse(request, "schema_admin.html", {"diagram": diagram})

    return router
