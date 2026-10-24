"""A song's metadata, collection context, actions and listening evidence."""
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from yt_playlist.core.accounts import catalog_client
from yt_playlist.library.album_fetch import is_upload_album
from yt_playlist.library.ratings import main_rating
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.thumbnails import best_thumb, detail_thumb


def build(ctx):
    router = APIRouter()

    # Register this after the existing /track/... literal and action routes.
    @router.get('/track/{video_id}')
    def track_page(request: Request, video_id: str, page: int = Query(0, ge=0, le=100000)):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', video_id):
            raise HTTPException(404, 'Track not found')
        track = ctx.store.tracks.track_detail(video_id)
        known = track is not None
        if track is None:
            hints = request.query_params
            track = {field: (hints.get(field) or '')[:1000] for field in
                     ('title', 'artist', 'album', 'album_browse', 'thumbnail', 'duration')}
            # Most discovery links already carry their preview. A bare shared URL can
            # still resolve live without acquiring library or discovery membership.
            if not track['title']:
                try:
                    client = catalog_client(ctx.client_provider())
                    details = (client.get_song(video_id) if client else {}).get('videoDetails') or {}
                    track.update(title=details.get('title') or '', artist=details.get('author') or '',
                                 duration=details.get('lengthSeconds'),
                                 thumbnail=best_thumb((details.get('thumbnail') or {}).get('thumbnails')))
                except Exception:
                    ctx.logger.info('Track preview unavailable for %s', video_id, exc_info=True)
            track['video_id'] = video_id
            track['playlists'] = []
        track['duration'] = parse_duration(track.get('duration'))
        for field in ('artist', 'album'):
            track[field] = track.get(field) or ''
        track['title'] = track.get('title') or 'Unknown track'
        track['thumbnail'] = track.get('thumbnail') or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
        rating = main_rating(ctx.store, video_id)
        ledger, more = ctx.store.tracks.track_play_ledger(track['recording_id'], page=page) if known else ([], False)
        for row in ledger:
            at = datetime.fromtimestamp(row['sort_at'], timezone.utc)
            row['at_iso'] = at.isoformat()
            row['at_label'] = at.strftime('%Y-%m-%d %H:%M UTC')
        browse = track.get('album_browse')
        return ctx.templates.TemplateResponse(request, 'track.html', {
            'track': track, 'cover': detail_thumb(track['thumbnail']),
            'known': known, 'rating': rating['status'] if rating else 'INDIFFERENT',
            'ledger': ledger, 'more': more, 'page': page,
            'saved': bool(browse and browse in ctx.store.collection.saved_album_ids()),
            'is_upload': bool(browse and is_upload_album(browse)),
        })

    return router
