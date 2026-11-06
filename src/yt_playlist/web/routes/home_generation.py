"""Materialize Home mixes and navigate the connected player after confirmation."""
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from yt_playlist.core.accounts import client_for
from yt_playlist.core.activity import activity_for
from yt_playlist.library import playlist_generated, playlist_navigation
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.rec import choice_evidence, measurement
from yt_playlist.web.context import request_form
from yt_playlist.web.jobs import start_playlist_enrichment
from yt_playlist.web import generated_playlist


def build(ctx):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates
    router.include_router(generated_playlist.build(ctx))

    @router.post('/home/generate/reconcile')
    def reconcile_generated(form=Depends(request_form)):
        """Read a known destination; absence in a read never authorizes replay."""
        try:
            recovery = json.loads(form.get('recovery') or '{}')
            tracks = json.loads(form.get('tracks') or '[]')
            client = (ctx.client_provider() or {}).get(int(recovery['account_id']))
            detail = validate_playlist(client.get_playlist(recovery['playlist'], limit=None))
            present = {track['videoId'] for track in detail['tracks']}
            confirmed = set(recovery['confirmed']) | present
            rejected = set(recovery['rejected']) - present
            unattempted = set(recovery['unattempted']) - present
            videos = list(dict.fromkeys(track['video_id'] for track in tracks))
            return JSONResponse({**recovery,
                'confirmed': [video for video in videos if video in confirmed],
                'rejected': [video for video in videos if video in rejected],
                'unattempted': [video for video in videos if video in unattempted],
                'uncertain': [video for video in videos
                              if video not in confirmed | rejected | unattempted]})
        except Exception:
            ctx.logger.exception('could not check saved mix')
            return JSONResponse(None)

    @router.post("/home/generate")
    def home_generate(request: Request, form=Depends(request_form)):
        """Materialize a proto-playlist as a real YouTube playlist, auto-grouped 'Generated' so the
        rec engine ignores it until it's played. The card's surviving rows are posted as `tracks`."""
        bridge = getattr(ctx, "bridge", None)
        intent = bridge.begin_playback_intent() if bridge is not None else None
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
            account_id,client = client_for(store,ctx.client_provider())
        except ValueError as exc:
            account_id,client,account_error = None,None,str(exc)
        result = {"name": name, "launch_id": form.get('launch_id') or ''}
        if client is None or not tracks:
            result["error"] = account_error or "Keep at least one track to create a playlist."
        else:
            try:
                res = activity_for(ctx).wrap(
                    "create generated playlist", playlist_generated.create_generated_playlist,
                    store, name, tracks, client, now_fn(), account_id, recipe=recipe)()
                if (res.get("pid") and isinstance(recipe, dict)
                        and (recipe.get("model") == "fresh" or recipe.get("lane") == "fresh")):
                    start_playlist_enrichment(ctx, res["pid"])
                # Land playback in the already-open YouTube Music tab, not a new one. The watch URL
                # (list=...) autoplays the playlist. But YouTube returns the new id before the
                # watch?list= page is loadable, so the switch waits until get_playlist confirms the
                # playlist exists (playlist_navigation.navigate_when_ready), on a background thread so this
                # response is not held for the poll. The result template never opens a YouTube tab.
                watch_url = f"https://music.youtube.com/watch?list={res['new_ytm']}"
                if bridge is not None and getattr(bridge, "connected", False):
                    activity_for(ctx).start_thread(playlist_navigation.navigate_when_ready,
                        args=(client, bridge, res["new_ytm"], watch_url), kwargs={"intent": intent})
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
                if res['pid'] is None:
                    receipt = ctx.jobs.create(kind='generated_playlist')
                    receipt.parameters = {'account_id': account_id, 'ytm': res['new_ytm']}
                    receipt.result = {**res, 'recipe': recipe}
                    receipt.done = True
                    result['playlist_url'] = f'/playlist/saved/{account_id}/{quote(res["new_ytm"], safe="")}'
                else:
                    result['playlist_url'] = f'/playlist/{res["pid"]}'
                if res.get("pid") and res.get("added"):
                    measurement.record_action(store, "save", now_fn(), playlist_id=res["pid"],
                                              event_id=f"generate:{res['new_ytm']}", generated=True)
                    if isinstance(recipe, dict) and recipe.get("opportunity_id") and recipe.get("option_id"):
                        for kind in ("choice", "save"):
                            choice_evidence.safe_respond(store, recipe["opportunity_id"], kind, now_fn(),
                                option_id=recipe["option_id"], action_id=f"generate:{res['new_ytm']}:{kind}",
                                details={"playlist_id": res["pid"]})
                        if ctx.rec_worker:
                            ctx.rec_worker.trigger_home_refresh()
                if isinstance(recipe, dict) and recipe.get("mode_id") is not None and res.get("pid"):
                    try:
                        store.modes.log_pick(res["pid"], int(recipe["mode_id"]), now_fn(),
                                             ranker=recipe.get("ranker"))
                    except Exception:  # noqa: BLE001 - logging must never break playlist creation
                        pass
            except PlaylistAddUncertain as exc:
                ctx.logger.warning('generated mix additions remain unconfirmed: %s', exc)
                result.update(error='Some songs are still unconfirmed.', partial_ytm=exc.playlist_id,
                    save_uncertain=True, selected_tracks=tracks,
                    addition_recovery={'account_id': account_id, 'playlist': exc.playlist_id,
                        'confirmed': list(exc.confirmed), 'rejected': list(exc.skipped),
                        'uncertain': list(exc.uncertain), 'unattempted': list(exc.unattempted)})
            except playlist_generated.TrackLengthLimitError as exc:
                result.update(error=str(exc), track_length_limited=True)
            except Exception:  # noqa: BLE001 - surface a friendly card, log the detail
                ctx.logger.exception("generate playlist %r failed", name)
                result.update(save_uncertain=True, error="We couldn’t confirm playback.",
                              selected_tracks=[track for track in tracks if isinstance(track, dict)]
                              if isinstance(tracks, list) else [])
        if (request.headers.get('HX-Request') == 'true' and form.get('artist_start') != '1'
                and result.get('ytm') and not result.get('error')):
            # Navigate before any card swap. Rendering the saved confirmation
            # inside the focused mix briefly exposes an incomplete playlist UI.
            return Response(headers={
                'HX-Redirect': result['playlist_url'],
                'HX-Trigger': json.dumps({'generated-playlist-saved': {
                    'launch_id': result['launch_id'], 'playlist': result['ytm']}}),
            })
        template = ('_partials/artist_playback.html' if form.get('artist_start') == '1'
                    else '_partials/generated_result.html')
        return templates.TemplateResponse(request, template, result)

    return router

