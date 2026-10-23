"""Materialize Home mixes and navigate the connected player after confirmation."""
import json

from fastapi import APIRouter, Depends, Request

from yt_playlist.core.accounts import client_for
from yt_playlist.core.activity import activity_for
from yt_playlist.library import playlist_generated, playlist_navigation
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.rec import measurement
from yt_playlist.web.context import request_form
from yt_playlist.web.jobs import start_playlist_enrichment


def build(ctx):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.post("/home/generate")
    def home_generate(request: Request, form=Depends(request_form)):
        """Materialize a proto-playlist as a real YouTube playlist, auto-grouped 'Generated' so the
        rec engine ignores it until it's played. The card's surviving rows are posted as `tracks`."""
        name = (form.get("name") or "").strip()
        try:
            tracks = json.loads(form.get("tracks") or "[]")
        except (ValueError, TypeError):
            tracks = []
        try:
            recipe = json.loads(form.get("recipe") or "null")
        except (ValueError, TypeError):
            recipe = None
        account_error = None
        try:
            identity_id,client = client_for(store,ctx.client_provider())
        except ValueError as exc:
            identity_id,client,account_error = None,None,str(exc)
        result = {"name": name}
        if client is None or not tracks:
            result["error"] = account_error or "Keep at least one track to create a playlist."
        else:
            try:
                res = activity_for(ctx).wrap(
                    "create generated playlist", playlist_generated.create_generated_playlist,
                    store, name, tracks, client, now_fn(), identity_id, recipe=recipe)()
                if (res.get("pid") and isinstance(recipe, dict)
                        and (recipe.get("model") == "fresh" or recipe.get("lane") == "fresh")):
                    start_playlist_enrichment(ctx, res["pid"])
                # Land playback in the already-open YouTube Music tab, not a new one. The watch URL
                # (list=...) autoplays the playlist. But YouTube returns the new id before the
                # watch?list= page is loadable, so the switch waits until get_playlist confirms the
                # playlist exists (playlist_navigation.navigate_when_ready), on a background thread so this
                # response is not held for the poll. The result template never opens a YouTube tab.
                watch_url = f"https://music.youtube.com/watch?list={res['new_ytm']}"
                bridge = getattr(ctx, "bridge", None)
                if bridge is not None and getattr(bridge, "connected", False):
                    activity_for(ctx).start_thread(playlist_navigation.navigate_when_ready,
                        args=(client, bridge, res["new_ytm"], watch_url))
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
                if res.get("pid") and res.get("added"):
                    measurement.record_action(store, "save", now_fn(), playlist_id=res["pid"],
                                              event_id=f"generate:{res['new_ytm']}", generated=True)
                if isinstance(recipe, dict) and recipe.get("mode_id") is not None and res.get("pid"):
                    try:
                        store.modes.log_pick(res["pid"], int(recipe["mode_id"]), now_fn(),
                                             ranker=recipe.get("ranker"))
                    except Exception:  # noqa: BLE001 - logging must never break playlist creation
                        pass
            except PlaylistAddUncertain as exc:
                result.update(error=str(exc), partial_ytm=exc.playlist_id)
            except Exception:  # noqa: BLE001 - surface a friendly card, log the detail
                ctx.logger.exception("generate playlist %r failed", name)
                result["error"] = "YouTube returned an unexpected response."
        return templates.TemplateResponse(request, "_partials/generated_result.html", result)

    return router

