"""Albums and Artists tabs (playlists and saved albums) + save/unsave album."""
from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for

from fastapi import APIRouter, Request
from fastapi.responses import Response

from yt_playlist.library import album_writes
from yt_playlist.rec import choice_evidence


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
        view = "playlists" if request.query_params.get("view") == "playlists" else "saved"
        data = {"view": view}
        if view == "saved":
            data["saved"] = store.collection.get_saved_albums()   # populated by sync (get_library_albums)
            data["saved_owners"] = store.collection.saved_album_owners()
        else:
            data["albums"] = store.collection.collection_albums()
        return templates.TemplateResponse(request, "albums.html", data)

    @router.get("/artists")
    def artists_page(request: Request):
        return templates.TemplateResponse(request, "artists.html", {"artists": store.collection.collection_artists()})

    def _album_and_client(browse_id, *, identity_id=None):
        identity_id,client = client_for(store,ctx.client_provider(),identity_id=identity_id)
        album = client.get_album(browse_id)
        if not album.get("audioPlaylistId"):
            raise ValueError("album has no playlist")
        return identity_id,client,album

    def _save_album(browse_id):
        identity_id,client,album = _album_and_client(browse_id)
        album_writes.set_saved(store, client, identity_id, browse_id, album, saved=True)

    def _unsave_album(browse_id, identity_id):
        owners = store.collection.saved_album_owners(browse_id).get(browse_id, [])
        if identity_id is None:
            if len(owners) != 1 or owners[0]['identity_id'] is None:
                raise ValueError("Choose which account to remove this album from.")
            identity_id = owners[0]['identity_id']
        if identity_id not in {owner['identity_id'] for owner in owners}:
            raise ValueError("This album is not saved in the selected account.")
        identity_id,client,album = _album_and_client(browse_id,identity_id=identity_id)
        album_writes.set_saved(store, client, identity_id, browse_id, album, saved=False)

    @router.post("/collection/save-album")
    async def save_album(request: Request):
        form = await request.form()
        browse_id = (form.get("browse_id") or "").strip()
        if not browse_id:
            return _toast(request, "no album id")
        try:
            # Lookup and rating both wait on replies delivered by the ASGI WebSocket loop.
            await activity_for(ctx).to_thread(_save_album, browse_id)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("save-album %s failed", browse_id)
            return _toast(request, "YouTube refused the save")
        if form.get("opportunity_id"):
            choice_evidence.safe_respond(store, form["opportunity_id"], "save", ctx.now_fn(), option_id=browse_id)
        if worker := getattr(ctx, 'rec_worker', None):
            worker.trigger_home_refresh()
        return _refresh()

    @router.post("/collection/unsave-album")
    async def unsave_album(request: Request):
        form = await request.form()
        browse_id = (form.get("browse_id") or "").strip()
        if not browse_id:
            return _toast(request, "no album id")
        try:
            try:
                identity_id = int(form['identity_id']) if form.get('identity_id') else None
            except ValueError:
                raise ValueError("Invalid account selection.") from None
            await activity_for(ctx).to_thread(_unsave_album, browse_id, identity_id)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("unsave-album %s failed", browse_id)
            return _toast(request, "YouTube refused")
        return _refresh()

    return router
