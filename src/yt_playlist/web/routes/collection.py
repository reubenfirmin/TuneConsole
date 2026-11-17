"""Albums and Artists tabs (playlists and saved albums) + save/unsave album."""
from yt_playlist.core.activity import activity_for
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.web.context import Ctx

from fastapi import APIRouter, Request
from fastapi.responses import Response



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
        data = Ctx.collection_commands(ctx).albums(view)
        return templates.TemplateResponse(request, "albums.html", data)

    @router.get("/artists")
    def artists_page(request: Request):
        return templates.TemplateResponse(request, "artists.html", {"artists": Ctx.collection_commands(ctx).artists()})

    @router.post("/collection/save-album")
    async def save_album(request: Request):
        form = await request.form()
        browse_id = (form.get("browse_id") or "").strip()
        if not browse_id:
            return _toast(request, "no album id")
        try:
            # Lookup and rating both wait on replies delivered by the ASGI WebSocket loop.
            await activity_for(ctx).to_thread(Ctx.collection_commands(ctx).save, browse_id, form.get('opportunity_id'))
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("save-album %s failed", browse_id)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        return _refresh()

    @router.post("/collection/unsave-album")
    async def unsave_album(request: Request):
        form = await request.form()
        browse_id = (form.get("browse_id") or "").strip()
        if not browse_id:
            return _toast(request, "no album id")
        try:
            try:
                account_id = int(form['identity_id']) if form.get('identity_id') else None
            except ValueError:
                raise ActionInputError('account_invalid') from None
            await activity_for(ctx).to_thread(Ctx.collection_commands(ctx).remove, browse_id, account_id)
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("unsave-album %s failed", browse_id)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        return _refresh()

    return router
