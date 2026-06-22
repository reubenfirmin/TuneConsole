"""Navbar omnisearch: a single typeahead endpoint that renders the results dropdown.

GET /search/omni?q=... -> the _partials/omni_results.html fragment (HTMX swaps it into
#omni-results). Read-only; all the matching/pivot logic lives in SearchRepo.omni_search.
"""
from fastapi import APIRouter, Request


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/search/omni")
    def omni(request: Request, q: str = ""):
        result = store.omni_search(q)
        return templates.TemplateResponse(request, "_partials/omni_results.html", result)

    return router
