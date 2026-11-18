"""TV message validation and transport mapping."""
import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from yt_playlist.controllers.tv_queries import playlist_items as query_items
from yt_playlist.rec import tv, tv_artwork
from yt_playlist.web.context import Ctx, request_form
from yt_playlist.web.routes.home_access import guard


def playlist_items(store, pid, *, artwork=True):
    with store.read_view() as reader:
        return query_items(reader, pid, artwork=artwork, images=tv_artwork.images)


def build(ctx):
    router = APIRouter()
    commands = Ctx.tv_commands(ctx)

    def call(command, *args, conflict=400):
        try:
            result = command(*args)
        except LookupError as error:
            return JSONResponse({'error': str(error)}, status_code=404)
        except ValueError as error:
            return JSONResponse({'error': str(error)}, status_code=conflict)
        return JSONResponse(result, status_code=202 if result.get('preparing') or result.get('pending') else 200)

    @router.post('/home/tv/playlist/{pid}/visual-check')
    def visual_check(pid: int, form=Depends(request_form)):
        return call(commands.visual_check, pid, form.get('video_id'))

    @router.get('/home/tv')
    def prepared():
        return commands.status()

    @router.get('/home/tv/artwork')
    def artwork(artist: str = ''):
        return commands.artist_artwork(artist)

    @router.post('/home/tv')
    def parameters(form=Depends(request_form)):
        return call(commands.parameters, form.get('max_minutes'))

    @router.post('/home/tv/play', dependencies=[Depends(guard(ctx, 'tv'))])
    def play(form=Depends(request_form)):
        return call(commands.play, form.get('request_id') or '')

    @router.post('/home/tv/reset')
    def reset():
        return commands.reset()

    @router.post('/home/tv/playlist/{pid}/refill')
    def refill(pid: int, form=Depends(request_form)):
        try:
            request_id, token = form.get('request_id') or '', form.get('stream') or ''
            if not request_id or len(request_id) > 64 or len(token) > 64:
                raise ValueError
            exclude, recent = json.loads(form.get('exclude') or '[]'), json.loads(form.get('recent') or '[]')
            if any(not isinstance(values, list) or len(values) > tv.MAX_POOL
                or any(not isinstance(video, str) or not tv._VIDEO.fullmatch(video) for video in values)
                for values in (exclude, recent)):
                raise ValueError
        except (ValueError, TypeError):
            return JSONResponse({'error': 'Invalid TV continuation.'}, status_code=400)
        try:
            return call(commands.refill, pid, token, request_id, exclude, recent, form.get('current'), conflict=409)
        except Exception:
            ctx.logger.exception('TV continuation failed')
            return JSONResponse({'error': 'The next videos could not load. Reset TV to start a fresh session.'}, status_code=502)

    @router.get('/home/tv/playlist/{pid}')
    def saved(pid: int):
        return call(commands.saved, pid)

    @router.post('/home/tv/playlist/{pid}/event')
    def playback_event(pid: int, form=Depends(request_form)):
        return call(commands.event, pid, form)

    @router.post('/home/tv/takeover')
    async def takeover():
        result = await commands.takeover()
        return JSONResponse(result, status_code=409 if result.get('error') else 200)

    return router
