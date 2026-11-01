"""Artist choices for the First sounds area, with optional Spotify help."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from yt_playlist.rec import artist_start, home_cards


class Selection(BaseModel):
    browse_id: str = Field(min_length=8, max_length=100)
    selected: bool


def build(ctx):
    router = APIRouter()
    catalog = artist_start.ArtistCatalog(ctx.client_provider)
    store = ctx.store

    def state():
        choices = store.discovery.starter_artists()
        songs = store.discovery.starter_tracks()
        return {'selected': choices, 'songs': len(songs), 'minimum': artist_start.MIN_ARTISTS,
                'complete': not artist_start.required(store),
                'ready': len(choices) >= artist_start.MIN_ARTISTS and bool(songs)}

    @router.get('/home/onboard/artists/search')
    def search(q: str = ''):
        try:
            return {'artists': catalog.search(q)}
        except (artist_start.CatalogUnavailable, TimeoutError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.get('/home/onboard/artists/state')
    def current_state():
        return state()

    @router.get('/home/onboard/artists/suggestions')
    def suggestions():
        try:
            return {'artists': catalog.related(store)}
        except (artist_start.CatalogUnavailable, TimeoutError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.post('/home/onboard/artists/select')
    def select(selection: Selection):
        try:
            related = catalog.select(store, selection.browse_id, selection.selected, ctx.now_fn())
        except (artist_start.CatalogUnavailable, TimeoutError) as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if selection.selected and ctx.rec_worker:
            ctx.rec_worker.trigger_mode_refresh()
        return {**state(), 'related': related}

    @router.get('/home/onboard/artists/listen')
    def listen(request: Request):
        tracks = store.discovery.starter_tracks()
        choices = store.discovery.starter_artists()
        ready = len(choices) >= artist_start.MIN_ARTISTS and bool(tracks)
        return ctx.templates.TemplateResponse(request, '_partials/artist_first_listen.html', {
            'proto': home_cards._proto(store, 'artist_start', 'My first set', tracks, ctx.now_fn()) if ready else None,
            'artists': choices})

    return router
