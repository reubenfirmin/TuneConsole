"""Genres tab: view and edit the genre whitelist that Last.fm/Discogs tags are matched against."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from yt_playlist import genres as genre_lib


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/genres")
    def genres_page(request: Request):
        return templates.TemplateResponse(request, "genres.html", {
            "genres": store.get_genre_whitelist(),
            "builtin_count": len(genre_lib.builtin_names()),
        })

    @router.post("/genres/add")
    async def genres_add(request: Request):
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "enter a genre name"})
        store.add_genre(name)
        genre_lib.configure(store)                     # rebuild the active matcher
        return JSONResponse({"ok": True, "genres": store.get_genre_whitelist()})

    @router.post("/genres/remove")
    async def genres_remove(request: Request):
        body = await request.json()
        store.remove_genre((body.get("name") or "").strip())
        genre_lib.configure(store)
        return JSONResponse({"ok": True, "genres": store.get_genre_whitelist()})

    @router.post("/genres/reset")
    def genres_reset():
        store.set_genres(genre_lib.builtin_names())
        genre_lib.configure(store)
        return JSONResponse({"ok": True, "genres": store.get_genre_whitelist()})

    return router
