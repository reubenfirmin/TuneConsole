"""A song's metadata, collection context, actions and listening evidence."""
import re

from fastapi import APIRouter, HTTPException, Query, Request

from yt_playlist.library import track_detail
from yt_playlist.library.album_fetch import is_upload_album
from yt_playlist.library.ratings import main_rating
from yt_playlist.util.thumbnails import detail_thumb
from yt_playlist.web.musical_evidence import describe


def build(ctx):
    router = APIRouter()

    @router.get('/track/{video_id}/history')
    def track_history(request: Request, video_id: str, page: int = Query(0, ge=0, le=100000)):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', video_id):
            raise HTTPException(404, 'Track not found')
        # Poll local listening evidence only. Opening a preview or pressing Play cannot create it.
        track = ctx.store.tracks.track_detail(video_id)
        return ctx.templates.TemplateResponse(request, '_partials/track_history.html',
                                              track_detail.history(ctx.store, video_id, page, track))

    # Register this after the existing /track/... literal and action routes.
    @router.get('/track/{video_id}')
    def track_page(request: Request, video_id: str, page: int = Query(0, ge=0, le=100000)):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', video_id):
            raise HTTPException(404, 'Track not found')
        track, known = track_detail.detail(ctx.store, ctx.client_provider, video_id,
                                           request.query_params, ctx.logger)
        rating = main_rating(ctx.store, video_id)
        history = track_detail.history(ctx.store, video_id, page, track if known else None)
        profile, sources, truncated = track_detail.profile_evidence(ctx.store, track, known)
        browse = track.get('album_browse')
        return ctx.templates.TemplateResponse(request, 'track.html', {
            'track': track, 'cover': detail_thumb(track['thumbnail']),
            'known': known, 'rating': rating['status'] if rating else 'INDIFFERENT',
            'ledger': history['ledger'], 'more': history['more'], 'page': page,
            'musical': describe(profile, sources, truncated=truncated),
            'saved': bool(browse and browse in ctx.store.collection.saved_album_ids()),
            'is_upload': bool(browse and is_upload_album(browse)),
        })

    return router
