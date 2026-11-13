"""Artist choices for the First sounds area, with optional Spotify help."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from yt_playlist.rec import artist_start, home_cards


class Selection(BaseModel):
    browse_id: str = Field(min_length=8, max_length=100)
    selected: bool


def build(ctx):
    router = APIRouter()

    def trigger_refresh():
        if ctx.rec_worker:
            ctx.rec_worker.trigger_mode_refresh()

    catalog = artist_start.ArtistCatalog(ctx.client_provider, trigger_refresh=trigger_refresh)
    store = ctx.store

    @router.get('/home/onboard/artists/search')
    def search(q: str = ''):
        try:
            return {'artists': catalog.search(q)}
        except (artist_start.CatalogUnavailable, TimeoutError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.get('/home/onboard/artists/state')
    def current_state():
        return catalog.state(store)

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
        return {**catalog.state(store), 'related': related}

    @router.get('/home/onboard/artists/listen')
    def listen(request: Request):
        context = catalog.listen_context(store)
        return ctx.templates.TemplateResponse(request, '_partials/artist_first_listen.html', {
            'proto': home_cards._proto(store, 'artist_start', 'My first set', context['tracks'], ctx.now_fn())
                     if context['ready'] else None,
            'artists': context['choices']})

    return router
