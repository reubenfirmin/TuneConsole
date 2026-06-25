"""Albums and Artists tabs (your collection across all playlists) + save/unsave album."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from yt_playlist.util.thumbnails import best_thumb


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _refresh():
        # htmx reloads on this header: exact parity with the old location.reload(), which keeps the
        # collection table, the saved column, and the discography table in sync after a save/unsave.
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

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

    def _album_and_client(browse_id):
        clients = ctx.client_provider() or {}
        client = next(iter(clients.values()), None)
        if client is None:
            raise ValueError("no client available")
        album = client.get_album(browse_id)
        if not album.get("audioPlaylistId"):
            raise ValueError("album has no playlist")
        return client, album

    @router.post("/collection/save-album")
    async def save_album(request: Request):
        # Save a YouTube album to your library: get_album -> like its audio playlist; mirror locally.
        browse_id = ((await request.form()).get("browse_id") or "").strip()
        if not browse_id:
            return _toast(request, "no album id")
        try:
            client, album = _album_and_client(browse_id)
            client.rate_playlist(album["audioPlaylistId"], "LIKE")
            store.add_saved_album({
                "browse": browse_id, "title": album.get("title"), "type": album.get("type"),
                "artist": ", ".join(x.get("name", "") for x in (album.get("artists") or [])),
                "year": album.get("year"), "thumbnail": best_thumb(album.get("thumbnails"))})
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("save-album %s failed", browse_id)
            return _toast(request, "YouTube refused the save")
        return _refresh()

    @router.post("/collection/unsave-album")
    async def unsave_album(request: Request):
        browse_id = ((await request.form()).get("browse_id") or "").strip()
        if not browse_id:
            return _toast(request, "no album id")
        try:
            client, album = _album_and_client(browse_id)
            client.rate_playlist(album["audioPlaylistId"], "INDIFFERENT")
            store.remove_saved_album(browse_id)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("unsave-album %s failed", browse_id)
            return _toast(request, "YouTube refused")
        return _refresh()

    return router
