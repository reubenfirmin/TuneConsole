"""Genres tab: view and edit the genre whitelist that Last.fm/Discogs tags are matched against."""
from fastapi import APIRouter, Request

from yt_playlist import genres as genre_lib


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _list(request):
        return templates.TemplateResponse(request, "_partials/genre_list.html",
                                          {"genres": store.get_genre_whitelist()})

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    @router.get("/genres")
    def genres_page(request: Request):
        return templates.TemplateResponse(request, "genres.html", {
            "genres": store.get_genre_whitelist(),
            "builtin_count": len(genre_lib.builtin_names()),
        })

    @router.post("/genres/add")
    async def genres_add(request: Request):
        form = await request.form()
        name = (form.get("name") or "").strip()
        if not name:
            return _toast(request, "enter a genre name")
        store.add_genre(name)
        genre_lib.configure(store)                     # rebuild the active matcher
        return _list(request)

    @router.post("/genres/remove")
    async def genres_remove(request: Request):
        form = await request.form()
        store.remove_genre((form.get("name") or "").strip())
        genre_lib.configure(store)
        return _list(request)

    @router.post("/genres/reset")
    def genres_reset(request: Request):
        store.set_genres(genre_lib.builtin_names())
        genre_lib.configure(store)
        return _list(request)

    return router
