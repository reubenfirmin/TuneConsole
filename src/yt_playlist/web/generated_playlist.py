"""Show a confirmed provider playlist before library sync can import its local row."""
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from yt_playlist.library.playlist_generated import saved_playlist_query
from yt_playlist.rec import onboarding
from yt_playlist.web.playlist_heading import title_context


def build(ctx):
    router = APIRouter()

    @router.get('/playlist/saved/{account_id}/{ytm}')
    def saved_playlist(request: Request, account_id: int, ytm: str):
        store = ctx.store
        local = store.playlists.get_playlist_for_account(account_id, ytm)
        if local is not None:
            return RedirectResponse(f'/playlist/{local.id}', status_code=303)
        try:
            saved = saved_playlist_query(store, ctx.jobs, ctx.client_provider, account_id, ytm)
        except ConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        tracks = saved['tracks']
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
