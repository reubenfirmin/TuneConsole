"""Albums and Artists tabs (your collection across all playlists) + save-album-to-library."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/albums")
    def albums_page(request: Request):
        view = "saved" if request.query_params.get("view") == "saved" else "playlists"
        data = {"view": view}
        if view == "saved":
            data["saved"] = store.get_saved_albums()   # populated by sync (get_library_albums)
        else:
            data["albums"] = store.collection_albums()
        return templates.TemplateResponse(request, "albums.html", data)

    @router.get("/artists")
    def artists_page(request: Request):
        return templates.TemplateResponse(request, "artists.html", {"artists": store.collection_artists()})

    @router.post("/collection/save-album")
    async def save_album(request: Request):
        # Save a YouTube album to your library: get_album -> like its audio playlist.
        form = await request.form()
        browse_id = (form.get("browse_id") or "").strip()
        if not browse_id:
            return JSONResponse({"ok": False, "error": "no album id"})
        clients = ctx.client_provider() or {}
        client = next(iter(clients.values()), None)
        if client is None:
            return JSONResponse({"ok": False, "error": "no client available"})
        try:
            album = client.get_album(browse_id)
            audio_pl = album.get("audioPlaylistId")
            if not audio_pl:
                return JSONResponse({"ok": False, "error": "album has no playlist to save"})
            client.rate_playlist(audio_pl, "LIKE")
        except Exception:  # noqa: BLE001
            ctx.logger.exception("save-album %s failed", browse_id)
            return JSONResponse({"ok": False, "error": "YouTube refused the save"})
        return JSONResponse({"ok": True, "title": album.get("title", "album")})

    return router
