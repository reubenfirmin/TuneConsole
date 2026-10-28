"""Serve prepared TV and materialize video playlists through the normal writer."""
import json
import math
import threading
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from yt_playlist.core.accounts import client_for
from yt_playlist.core.activity import activity_for
from yt_playlist.library.playlist_generated import create_generated_playlist
from yt_playlist.library import live_plays, player_events
from yt_playlist.library import tv_stream
from yt_playlist.rec import measurement, tv, tv_artwork, tv_visual
from yt_playlist.web.context import request_form


def playlist_items(store, pid, *, artwork=True):
    playlist = store.playlists.get_playlist(pid)
    recipe = store.playlist_recipes.get_recipe(playlist.scope_ref) if playlist else None
    if not isinstance(recipe, dict) or recipe.get('media') != 'video':
        return None
    metadata = {t['video_id']: t for t in recipe.get('videos', [])}
    tracks = store.charts.playlist_tracks_detail(pid)
    items = [{**metadata.get(t['video_id'], {}), **t,
                       'duration': t.get('duration') or metadata.get(t['video_id'], {}).get('duration', 0),
                       'likeStatus': 'LIKE' if t.get('liked') else 'INDIFFERENT'}
                      for t in tracks if t.get('video_id') and t.get('available') != 0]
    if not artwork:
        return playlist, items
    # Read artist photography only; optional provider work belongs to the worker.
    art = {name: tv_artwork.images(store, name) for name in
           {t.get('source_artist') or t['artist'] for t in items}}
    for item in items:
        images, prepared = art[item.get('source_artist') or item['artist']]
        item['artist_art'] = images
        item['artist_art_pending'] = not prepared
    return playlist, items


def build(ctx):
    router = APIRouter()
    refill_lock = threading.Lock()
    visual_inspector = tv_visual.Inspector()

    @router.post('/home/tv/playlist/{pid}/visual-check')
    def visual_check(pid: int, form=Depends(request_form)):
        contents = playlist_items(ctx.store, pid, artwork=False)
        if contents is None:
            return JSONResponse({'error': 'Video playlist not found.'}, status_code=404)
        playlist, items = contents
        item = next((item for item in items if item['video_id'] == form.get('video_id')), None)
        if item is None:
            return JSONResponse({'error': 'Video not in this playlist.'}, status_code=400)
        if getattr(ctx.store, 'activity_paused', False):
            return JSONResponse({'preparing': True}, status_code=202)
        _, client = client_for(ctx.store, ctx.client_provider(), playlist_id=pid)
        result = visual_inspector.inspect(ctx.store, client, item, ctx.now_fn())
        if result is None:
            return JSONResponse({'preparing': True}, status_code=202)
        return {'video_id': item['video_id'], **result}

    def preparation_message():
        pool = tv.cached(ctx.store) or {}
        if (ctx.store.rec_proposals.get_proposals('tv_preparation') or {}).get('failed'):
            return 'TV couldn’t reach the video catalog. We’re reconnecting; you can also reset TV to try again.'
        if pool.get('state') == 'no_sources':
            return 'TV needs a few liked songs or discoveries to find videos. Your library is still being prepared.'
        if pool.get('state') == 'exhausted' and not tv.queue(ctx.store, ctx.now_fn()):
            if tv.eligible(ctx.store, pool.get('items', []), None, ctx.now_fn()):
                return 'No playable videos matched this length. Try a longer video length, or reset TV to search again.'
            return 'No playable videos matched your current TV choices. Reset TV to search again.'
        return None

    def status():
        # No search, ranking, playlist creation or preview on a read.
        ready = bool(tv.queue(ctx.store, ctx.now_fn()))
        if not ready and ctx.rec_worker:
            ctx.rec_worker.trigger_tv_refresh()
        result = {'ready': ready, 'max_minutes': tv.max_minutes(ctx.store)}
        if not ready and (message := preparation_message()):
            result['message'] = message
        return result

    @router.get('/home/tv')
    def prepared():
        return status()

    @router.get('/home/tv/artwork')
    def artwork(artist: str = ''):
        if not artist or len(artist) > 200:
            return {'images': [], 'pending': False}
        images, prepared = tv_artwork.images(ctx.store, artist)
        pending = not prepared and tv_artwork.request(ctx.store, artist, ctx.now_fn())
        if pending and ctx.rec_worker:
            ctx.rec_worker.trigger_tv_refresh()
        return {'images': images, 'pending': pending}

    @router.post('/home/tv')
    def parameters(form=Depends(request_form)):
        value = form.get('max_minutes')
        try:
            maximum = None if value == 'unlimited' else int(value)
        except (ValueError, TypeError):
            return JSONResponse({'error': 'Choose a video length or Unlimited.'}, status_code=400)
        if maximum is not None and maximum < 1:
            return JSONResponse({'error': 'Choose at least one minute, or Unlimited.'}, status_code=400)
        ctx.store.settings.set_setting(tv.SETTING, 'unlimited' if maximum is None else str(maximum))
        return status()

    @router.post('/home/tv/play')
    def play():
        items = tv.queue(ctx.store, ctx.now_fn())
        if not items:
            if ctx.rec_worker:
                ctx.rec_worker.trigger_tv_refresh()
            result = {'preparing': True}
            if message := preparation_message():
                result['message'] = message
            return JSONResponse(result, status_code=202)
        try:
            identity_id, client = client_for(ctx.store, ctx.client_provider())
            recipe = {'lane': 'tv', 'media': 'video', 'max_minutes': tv.max_minutes(ctx.store),
                      'videos': items, 'tv_stream': uuid.uuid4().hex}
            result = activity_for(ctx).wrap('create TV playlist', create_generated_playlist,
                ctx.store, 'TV', items, client, ctx.now_fn(), identity_id, recipe=recipe,
                max_duration_s=math.inf if recipe['max_minutes'] is None else recipe['max_minutes'] * 60)()
            playlist, videos = playlist_items(ctx.store, result['pid'])
            tv.record_draw(ctx.store, videos)
            measurement.record_action(ctx.store, 'save', ctx.now_fn(), playlist_id=playlist.id,
                                      event_id=f"generate:{result['new_ytm']}", generated=True)
            return {'playlist_id': playlist.id, 'title': playlist.title, 'items': videos, 'stream': recipe['tv_stream']}
        except ValueError as error:
            return JSONResponse({'error': str(error)}, status_code=400)
        except Exception:
            ctx.logger.exception('TV playlist creation failed')
            return JSONResponse({'error': 'Couldn’t generate the TV session. Try again shortly.'},
                                status_code=502)

    @router.post('/home/tv/reset')
    def reset():
        if pool := tv.cached(ctx.store):
            ctx.store.rec_proposals.put_proposals(tv.SURFACE, {**pool, 'retry_at': 0, 'state': 'searching'}, ctx.now_fn())
        if ctx.rec_worker:
            ctx.rec_worker.trigger_tv_refresh()
        return {'ok': True}

    @router.post('/home/tv/playlist/{pid}/refill')
    def refill(pid: int, form=Depends(request_form)):
        playlist = ctx.store.playlists.get_playlist(pid)
        recipe = ctx.store.playlist_recipes.get_recipe(playlist.scope_ref) if playlist else None
        if not isinstance(recipe, dict) or recipe.get('media') != 'video':
            return JSONResponse({'error': 'This TV playlist is no longer available.'}, status_code=404)
        try:
            request_id = form.get('request_id') or ''
            token = form.get('stream') or ''
            if not request_id or len(request_id) > 64 or len(token) > 64:
                raise ValueError
            exclude = json.loads(form.get('exclude') or '[]')
            recent = json.loads(form.get('recent') or '[]')
            if any(not isinstance(values, list) or len(values) > tv.MAX_POOL
                   or any(not isinstance(video, str) or not tv._VIDEO.fullmatch(video) for video in values)
                   for values in (exclude, recent)):
                raise ValueError
        except (ValueError, TypeError):
            return JSONResponse({'error': 'Invalid TV continuation.'}, status_code=400)
        if not refill_lock.acquire(blocking=False):
            return JSONResponse({'preparing': True}, status_code=202)
        try:
            identity_id, client = client_for(ctx.store, ctx.client_provider(), playlist_id=pid)
            items = tv.queue(ctx.store, ctx.now_fn(), exclude=exclude, recent=recent,
                             current=form.get('current'), limit=4)
            next_pid, stream, confirmed = activity_for(ctx).wrap('continue TV playlist', tv_stream.refill,
                ctx.store, client, identity_id, playlist, recipe, token, request_id, items, ctx.now_fn())()
            next_playlist, videos = playlist_items(ctx.store, next_pid)
            metadata = {t['video_id']: t for t in videos}
            videos = [metadata[t['video_id']] for t in confirmed if t['video_id'] in metadata]
            if ctx.rec_worker:
                ctx.rec_worker.trigger_tv_refresh()
            return {'playlist_id': next_pid, 'title': next_playlist.title, 'items': videos, 'stream': stream}
        except tv_stream.PendingTV:
            if ctx.rec_worker:
                ctx.rec_worker.trigger_tv_refresh()
            return JSONResponse({'preparing': True}, status_code=202)
        except ValueError as error:
            return JSONResponse({'error': str(error)}, status_code=409)
        except Exception:
            ctx.logger.exception('TV continuation failed')
            return JSONResponse({'error': 'The next videos could not load. Reset TV to start a fresh session.'}, status_code=502)
        finally:
            refill_lock.release()

    @router.get('/home/tv/playlist/{pid}')
    def saved(pid: int):
        contents = playlist_items(ctx.store, pid)
        if contents is None:
            return JSONResponse({'error': 'This video playlist is no longer available.'}, status_code=404)
        playlist, items = contents
        return {'playlist_id': playlist.id, 'title': playlist.title, 'items': items}

    @router.post('/home/tv/playlist/{pid}/event')
    def playback_event(pid: int, form=Depends(request_form)):
        if getattr(ctx.store, 'activity_paused', False):
            return {'ok': True}
        contents = playlist_items(ctx.store, pid, artwork=False)
        if contents is None:
            return JSONResponse({'error': 'Video playlist not found.'}, status_code=404)
        playlist, items = contents
        kind = form.get('kind')
        item = next((t for t in items if t['video_id'] == form.get('video_id')), None)
        if kind not in {'play', 'state', 'tick', 'track_exit', 'ended', 'bye', 'player_error'} or item is None:
            return JSONResponse({'error': 'Invalid video playback event.'}, status_code=400)
        identity = next((i for i in ctx.store.identities.get_identities() if i.id == playlist.identity_id), None)
        if identity is None:
            return JSONResponse({'error': 'Playlist account not found.'}, status_code=404)
        try:
            position = float(form.get('position') or 0)
            if not math.isfinite(position) or position < 0:
                raise ValueError
        except (TypeError, ValueError):
            return JSONResponse({'error': 'Invalid playback position.'}, status_code=400)
        if kind == 'player_error':
            try:
                code = int(form.get('state'))
                if not 0 <= code <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                return JSONResponse({'error': 'Invalid player error code.'}, status_code=400)
            ctx.logger.warning('TV player error: playlist=%s video=%s code=%s position=%.3f',
                               pid, item['video_id'], code, position)
            return {'ok': True}
        # Use saved, confirmed metadata and account ownership rather than credits
        # supplied by the browser. These are the same handlers used by audio.
        msg = {'kind': kind, 'videoId': item['video_id'], 'title': item['title'],
               'artist': item['artist'], 'playlist': playlist.ytm_playlist_id,
               'brandId': identity.brand_account_id or '', 'brandKnown': True,
               'position': position, 'duration': item['duration'],
               'state': form.get('state'), 'timeZone': form.get('time_zone')}
        handler = live_plays.handle_play_event if kind == 'play' else player_events.handle_player_event
        activity_for(ctx).wrap('TV playback event', handler, ctx, msg, ctx.now_fn())()
        return {'ok': True}

    @router.post('/home/tv/takeover')
    async def takeover():
        try:
            status_code, body = await ctx.bridge.request_control({'type': 'pause-for-tv'}, timeout=8)
            result = json.loads(body)
            if status_code == 200 and result.get('ok') and result.get('paused') is True:
                return {'ok': True}
        except Exception:
            ctx.logger.warning('TV playback handoff was not confirmed', exc_info=True)
        return JSONResponse({'error': 'Couldn’t pause the audio player. Reconnect or reload the browser extension, then try TV again.'},
                            status_code=409)

    return router
