"""Album HTTP validation and presentation over complete application commands."""
import json
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.activity import activity_for
from yt_playlist.web.album_art import cover_and_mosaic
from yt_playlist.web.context import Ctx
from yt_playlist.util.navigation import playlist_url


def build(ctx):
    router = APIRouter()
    commands = Ctx.album_commands(ctx)
    templates = ctx.templates

    def toast(request, message):
        return templates.TemplateResponse(request, '_partials/error_toast.html', dict(message=message),
            status_code=422, headers={'HX-Reswap': 'none'})

    @router.get('/album/track-plays')
    def track_plays(request: Request):
        videos = list(dict.fromkeys(filter(None, request.query_params.get('videos', '').split(','))))
        if len(videos) > 500 or any(len(video) > 128 for video in videos):
            raise HTTPException(400, 'Too many track IDs')
        return JSONResponse(dict(plays=commands.plays(videos)), headers={'Cache-Control': 'no-store'})

    @router.get('/album/inline')
    def album_inline(request: Request, browse: str, opportunity: str = ''):
        return templates.TemplateResponse(request, '_partials/album_expansion.html',
            commands.context(browse, opportunity, inline=True))

    @router.get('/album')
    def album_page(request: Request):
        return templates.TemplateResponse(request, 'album.html', commands.context(
            (request.query_params.get('browse') or '').strip(), request.query_params.get('opportunity'), opened=True))

    @router.post('/album/feedback')
    async def album_feedback(request: Request):
        form = await request.form()
        try:
            commands.set_feedback((form.get('browse_id') or '').strip(), form.get('kind'), form.get('opportunity_id'))
        except ActionInputError as exc:
            return toast(request, str(exc))
        except Exception:
            ctx.logger.exception('Album feedback failed')
            return Response(status_code=503, headers={'HX-Reswap': 'none'})
        return Response(status_code=204)

    @router.post('/album/play')
    async def album_play(request: Request):
        form = await request.form()
        browse = (form.get('browse_id') or '').strip()
        if not browse:
            return toast(request, 'Choose an album to play.')
        intent = commands.admit_play()
        try:
            listening = await activity_for(ctx).to_thread(commands.play, browse, form.get('opportunity_id'),
                (form.get('action_id') or uuid.uuid4().hex).strip(), intent)
        except Exception:
            ctx.logger.exception('Album playback failed: %s', browse)
            return Response(status_code=503, headers={'HX-Reswap': 'none'})
        if listening is None or not commands.play_is_current(intent):
            return Response(status_code=204)
        return Response(status_code=204, headers={'HX-Trigger': json.dumps({'album-play-started': listening})})

    @router.get('/album/{browse}/share.txt')
    def album_share(browse: str):
        try:
            title, videos = commands.share(browse)
        except ActionInputError as exc:
            raise HTTPException(503, str(exc)) from None
        except Exception:
            ctx.logger.info('album share fetch failed for %r', browse, exc_info=True)
            raise HTTPException(503, 'The full album could not be fetched. Reconnect to YouTube Music and try sharing again.') from None
        safe = re.sub(r'[\\/:*?"<>|]', '_', title or 'album').strip() or 'album'
        ascii_name = safe.encode('ascii', 'ignore').decode() or 'album'
        return PlainTextResponse('\n'.join(f'https://music.youtube.com/watch?v={v}' for v in videos) + '\n',
            headers={'Content-Disposition': f'attachment; filename="{ascii_name}.txt"; filename*=UTF-8\'\'{quote(safe)}.txt'})

    def row(request, browse, video):
        data = Ctx.library_actions(ctx).album_row(browse, video)
        return templates.TemplateResponse(request, '_partials/album_track_row.html', data) if data else Response(status_code=204)

    async def edit(request, browse, field):
        form = await request.form()
        video = (form.get('video_id') or '').strip()
        try:
            Ctx.library_actions(ctx).edit_field(video, field, (form.get(field) or '').strip())
        except ValueError as exc:
            return toast(request, str(exc))
        return row(request, browse, video)

    @router.post('/album/{browse}/track-genre')
    async def album_set_track_genre(browse: str, request: Request):
        return await edit(request, browse, 'genre')

    @router.post('/album/{browse}/track-year')
    async def album_set_track_year(browse: str, request: Request):
        return await edit(request, browse, 'year')

    @router.post('/album/create-playlist')
    async def album_create_playlist(request: Request):
        form = await request.form()
        browse, name = (form.get('browse_id') or '').strip(), (form.get('name') or '').strip()
        if not browse:
            return toast(request, 'No album to create from.')
        try:
            result = await activity_for(ctx).to_thread(ctx.ops().create_playlist_from_album, browse, name)
        except ActionInputError as exc:
            return toast(request, str(exc))
        except Exception:
            ctx.logger.exception('create playlist from album %r failed', browse)
            return Response(status_code=503, headers={'HX-Reswap': 'none'})
        return Response(headers={'HX-Redirect': playlist_url(result['db_pid'])})

    @router.post('/album/{browse}/enrich')
    def album_enrich(browse: str):
        return JSONResponse(dict(job_id=Ctx.enrichment_commands(ctx).start(album_browse=browse).id))

    def conflicts(request, browse):
        return templates.TemplateResponse(request, '_partials/conflicts_modal.html',
            {**Ctx.enrichment_commands(ctx).conflicts(album_browse=browse), 'resolve_url': f'/album/{browse}/conflicts/resolve'})

    @router.get('/album/{browse}/conflicts')
    def album_conflicts(request: Request, browse: str):
        return conflicts(request, browse)

    @router.post('/album/{browse}/conflicts/resolve')
    async def album_resolve_conflicts(request: Request, browse: str):
        form = await request.form()
        Ctx.enrichment_commands(ctx).resolve_many(list(form.multi_items()))
        return conflicts(request, browse)

    return router
