"""Materialize Home mixes and navigate the connected player after confirmation."""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from yt_playlist.controllers.generated_playlists import MixSaveIncomplete
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.activity import activity_for
from yt_playlist.library import playlist_generated
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.web.context import request_form
from yt_playlist.web import generated_playlist


def build(ctx):
    router = APIRouter()
    templates = ctx.templates
    router.include_router(generated_playlist.build(ctx))

    @router.post('/home/generate/reconcile')
    def reconcile_generated(form=Depends(request_form)):
        """Look up a save receipt or check additions at a known destination."""
        if launch_id := form.get('launch_id'):
            receipt = ctx.jobs.latest('mix_save_receipt', launch_id)
            return JSONResponse(receipt.result if receipt else None)
        try:
            recovery = json.loads(form.get('recovery') or '{}')
            tracks = json.loads(form.get('tracks') or '[]')
            return JSONResponse(ctx.generated_playlists().reconcile(recovery, tracks))
        except Exception:
            ctx.logger.exception('could not check saved mix')
            return JSONResponse(None)

    @router.post("/home/generate")
    def home_generate(request: Request, form=Depends(request_form)):
        """Materialize a proto-playlist as a real YouTube playlist, auto-grouped 'Generated' so the
        rec engine ignores it until it's played. The card's surviving rows are posted as `tracks`."""
        commands = ctx.generated_playlists()
        intent = commands.admit()
        name = (form.get("name") or "").strip()
        try:
            tracks = json.loads(form.get("tracks") or "[]")
        except (ValueError, TypeError):
            tracks = []
        try:
            recipe = json.loads(form.get("recipe") or "null")
        except (ValueError, TypeError):
            recipe = None
        result = {"name": name, "launch_id": form.get('launch_id') or ''}
        if not tracks:
            result["error"] = "Keep at least one track to create a playlist."
        else:
            try:
                res = activity_for(ctx).wrap('create generated playlist', commands.home,
                    name, tracks, recipe, intent=intent)()
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
                result['destination_url'] = res['playlist_url']
                if form.get('first_sounds') == '1' and res.get('added'):
                    try:
                        ctx.first_sounds.begin(res)
                    except Exception:
                        ctx.logger.exception('could not retain First sounds continuation')
                if res['status'] == 'rejected':
                    result['error'] = 'No songs were added to this playlist.'
            except PlaylistAddUncertain as exc:
                ctx.logger.warning('generated mix additions remain unconfirmed: %s', exc)
                result.update(error='Some songs are still unconfirmed.', partial_ytm=exc.playlist_id,
                    save_uncertain=True, selected_tracks=exc.selected_tracks,
                    addition_recovery={'account_id': exc.account_id, 'playlist': exc.playlist_id,
                        'confirmed': list(exc.confirmed), 'rejected': list(exc.skipped),
                        'uncertain': list(exc.uncertain), 'unattempted': list(exc.unattempted)})
            except MixSaveIncomplete as exc:
                ctx.logger.exception('generated mix launch failed after creating playlist %s', exc.recovery['playlist'])
                result.update(error='We couldn’t confirm playback.', save_uncertain=True,
                              partial_ytm=exc.recovery['playlist'], selected_tracks=exc.tracks,
                              addition_recovery=exc.recovery)
            except playlist_generated.TrackLengthLimitError as exc:
                result.update(error=str(exc), track_length_limited=True)
            except ActionInputError as exc:
                result['error'] = str(exc)
            except Exception:  # noqa: BLE001 - surface a friendly card, log the detail
                ctx.logger.exception("generate playlist %r failed", name)
                result.update(save_uncertain=True, error="We couldn’t confirm playback.",
                              selected_tracks=[track for track in tracks if isinstance(track, dict)]
                              if isinstance(tracks, list) else [])
        if request.headers.get('HX-Request') == 'true' and form.get('mix_card') == '1':
            recovery = None
            if result.get('save_uncertain'):
                recovery = {'recovery': result.get('addition_recovery'), 'tracks': [
                    {'video_id': track['video_id'], 'recording_id': track.get('recording_id')}
                    for track in result['selected_tracks'] if isinstance(track, dict) and track.get('video_id')
                ]}
            if result['launch_id']:
                saved = {'saved': result['ytm'], 'added': result['added']} if result.get('ytm') and not result.get('error') else None
                if saved or (recovery and recovery['recovery']):
                    receipt = ctx.jobs.create(kind='mix_save_receipt')
                    receipt.parameters = result['launch_id']
                    receipt.result, receipt.done = saved or recovery, True
            if recovery:
                return JSONResponse(recovery, headers={'HX-Reswap': 'none', 'X-Mix-Recovery': '1'})
        if (request.headers.get('HX-Request') == 'true' and form.get('artist_start') != '1'
                and result.get('ytm') and not result.get('error')):
            # Navigate before any card swap. Rendering the saved confirmation
            # inside the focused mix briefly exposes an incomplete playlist UI.
            return Response(headers={
                'HX-Redirect': result['destination_url'],
                'HX-Trigger': json.dumps({'generated-playlist-saved': {
                    'launch_id': result['launch_id'], 'playlist': result['ytm']}}),
            })
        template = ('_partials/artist_playback.html' if form.get('artist_start') == '1'
                    else '_partials/generated_result.html')
        return templates.TemplateResponse(request, template, result)

    return router

