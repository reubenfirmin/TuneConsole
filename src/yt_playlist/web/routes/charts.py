"""Analytics resources: decode inputs, call complete queries, render supplied facts."""
from zoneinfo import ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from yt_playlist.library.analytics_composition import queries
from yt_playlist.web import analytics_views as views
from yt_playlist.web.analytics_views import more_albums as _more_albums
from yt_playlist.rec.ticker import periods as _ticker_periods

_WINDOWS = {'all': None, '90d': 90, '30d': 30, '7d': 7}
_TICKER_DIMS = ('genre', 'year', 'album', 'playlist', 'artist')


def build(ctx) -> APIRouter:
    from yt_playlist.core.features import listening_charts_enabled

    router, templates = APIRouter(), ctx.templates
    commands = queries(ctx)
    listening_enabled = listening_charts_enabled()

    @router.post('/charts/listening-impressions')
    async def listening_impressions(request: Request):
        if not listening_enabled:
            return Response(status_code=404)
        try:
            body = await request.json()
        except ValueError:
            return Response(status_code=400)
        if not isinstance(body, dict):
            return Response(status_code=204)
        if not commands.impressions(body.get('edition'), body.get('stories', [])):
            return Response(status_code=400)
        return Response(status_code=204)

    @router.get('/charts/listening-data')
    def listening_data():
        if not listening_enabled:
            raise HTTPException(404)
        encoded = commands.encoded()
        if encoded is None:
            return JSONResponse({'status': 'preparing'}, status_code=202,
                                headers={'Cache-Control': 'no-store'})
        return Response(encoded, media_type='application/json', headers={'Cache-Control': 'no-store'})

    @router.get('/charts/listening-chunk/{key}')
    def listening_chunk(key: str):
        if not listening_enabled or len(key) != 24 or any(c not in '0123456789abcdef' for c in key):
            raise HTTPException(404)
        encoded = commands.encoded(key)
        if encoded is None:
            raise HTTPException(404, 'This generation has changed. Reload the selection.')
        return Response(encoded, media_type='application/json',
                        headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    @router.post('/charts/listening-timezone')
    async def listening_timezone(request: Request):
        if not listening_enabled:
            raise HTTPException(404)
        try:
            body = await request.json()
            name = commands.timezone(body.get('timezone') if isinstance(body, dict) else None)
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(422, 'Choose a valid IANA timezone.')
        return JSONResponse({'timezone': name}, status_code=202)

    @router.get('/charts')
    def charts_page(request: Request):
        requested = request.query_params.get('view')
        if not listening_enabled and requested in ('home', 'analytics'):
            return RedirectResponse('/charts', status_code=303)
        prepared = commands.prepared() if listening_enabled else {'analytics': {}, 'edition': {}}
        available, edition = prepared['analytics'].get('available', False), prepared['edition']
        if requested == 'home' and not edition.get('available'):
            return RedirectResponse('/charts', status_code=303)
        initial = requested if requested in ('home', 'analytics', 'songs', 'artists', 'genres', 'years', 'albums', 'playlists') else (
            'home' if edition.get('available') else 'songs')
        win = request.query_params.get('window', 'all')
        context = {'initial_view': initial, 'listening_edition': edition, 'listening_available': available,
                   'window': win if win in _WINDOWS else 'all'}
        if initial in ('home', 'analytics'):
            return templates.TemplateResponse(request, 'charts.html', {
                'songs': [], 'tickers': {dim: {'rows': []} for dim in _TICKER_DIMS},
                'prepared_only': True, **context})
        return templates.TemplateResponse(request, 'charts.html', {
            **views.rankings(commands.rankings(_WINDOWS.get(win))), **context})

    @router.get('/artist')
    def artist_page(request: Request):
        facts = commands.artist((request.query_params.get('name') or '').strip(),
            (request.query_params.get('browse') or '').strip() or None, request.query_params.get('artist_id'))
        if facts is None:
            raise HTTPException(status_code=404, detail='no artist specified')
        return templates.TemplateResponse(request, 'artist.html', views.artist(facts))

    return router
