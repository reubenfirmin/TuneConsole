"""Show a confirmed provider playlist before library sync can import its local row."""
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.rec import onboarding
from yt_playlist.util.matching import track_album, track_artist
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.web.playlist_heading import title_context


def build(ctx):
    router = APIRouter()

    @router.get('/playlist/saved/{account_id}/{ytm}')
    def saved_playlist(request: Request, account_id: int, ytm: str):
        store = ctx.store
        local = store.playlists.get_playlist_for_account(account_id, ytm)
        if local is not None:
            return RedirectResponse(f'/playlist/{local.id}', status_code=303)
        # Reuse the bounded job registry for the confirmed save receipt. This
        # avoids waiting for provider indexing or writing to a read-only DB.
        receipt = ctx.jobs.latest('generated_playlist', {'account_id': account_id, 'ytm': ytm})
        if receipt is not None:
            saved = receipt.result
        else:
            # The stable link still works after a restart or receipt expiry.
            client = (ctx.client_provider() or {}).get(account_id)
            if client is None:
                raise HTTPException(503, 'Connect YouTube Music to open this playlist.')
            detail = validate_playlist(client.get_playlist(ytm, limit=None))
            saved = {'title': detail.get('title') or 'Your Mix Tape', 'recipe': None,
                     'tracks': [{'video_id': t['videoId'], 'title': t.get('title') or '',
                                 'artist': track_artist(t), 'album': track_album(t),
                                 'duration': t.get('duration_seconds'),
                                 'thumbnail': best_thumb(t.get('thumbnails'))} for t in detail['tracks']]}
        known = {t['video_id']: t for t in store.charts.video_tracks_detail(
            [t['video_id'] for t in saved['tracks']])}
        tracks = [{**{'album': '', 'genre': '', 'year': None, 'plays': 0, 'liked': False,
                       'identity_key': '', 'thumbnail': None, 'duration': None},
                   **t, **known.get(t['video_id'], {})} for t in saved['tracks']]
        playlist = SimpleNamespace(id=None, account_id=account_id, provider_playlist_id=ytm,
                                   title=saved['title'], thumbnail=next(
                                       (t['thumbnail'] for t in tracks if t['thumbnail']), None))
        return ctx.templates.TemplateResponse(request, 'playlist.html', {
            'pl': playlist, 'tracks': tracks, 'is_generated': True,
            **title_context(playlist.title, is_generated=True, recipe=saved.get('recipe')),
            'recipe': saved.get('recipe'), 'is_video': False,
            'show_playlist_radio': not onboarding.initial_setup_pending(store, ctx.now_fn()),
            'total_plays': sum(t['plays'] or 0 for t in tracks), 'genres': [],
            'facets': None, 'mood_states': {},
            'conflict_count': 0, 'position_pending': 0,
        })

    return router
