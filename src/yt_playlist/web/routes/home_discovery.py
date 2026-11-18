"""Prepared discovery/story reads and explicit visible-story measurements."""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from yt_playlist.rec import album_tuning, home_prepared, into_recently
from yt_playlist.web.context import request_form
from yt_playlist.web.routes.home_access import guard, state as growth_state
from yt_playlist.controllers.album_menu import familiarity as _familiarity
from yt_playlist.library.home_album_composition import album_menu
from yt_playlist.library.home_query_composition import navigation


class StoryImpressions(BaseModel):
    subjects: list[str] = Field(max_length=into_recently.MAX_STORIES)


class ChoiceResponse(BaseModel):
    opportunity_id: str = Field(max_length=64)
    option_id: str = Field(max_length=512)
    kind: str = Field(max_length=20)


def story_fragment(ctx, request):
    """Render the prepared story pool after the caller establishes access."""
    schedule = ctx.rec_worker.trigger_story_refresh if ctx.rec_worker else lambda: None
    context = navigation(ctx).story_query(ctx.now_fn())
    cards, preparing = context['cards'], context['preparing']
    if cards or preparing:
        return ctx.templates.TemplateResponse(request, "_partials/into_recently.html", context)
    return Response(status_code=200)


def album_context(ctx, views, now):
    return menu_commands(ctx, views).query(now)


def menu_commands(ctx, views):
    return album_menu(views.store, views.writer, now_playing=lambda: ctx.bridge.now_playing or {},
                      schedule=views.schedule, worker_available=ctx.rec_worker is not None, logger=ctx.logger)


def build(ctx, views):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates
    menu = menu_commands(ctx, views)
    commands = navigation(ctx)

    @router.post("/home/format", status_code=204)
    def home_format(request: Request, form=Depends(request_form)):
        preference, panel = form.get("format"), form.get("panel")
        status = commands.choose(preference, panel, lambda: growth_state(ctx, request))
        return Response('Wait until all three listening formats are ready.' if status == 409 and preference is not None
                        else '', status_code=status)

    @router.get("/home/album-choices", dependencies=[Depends(guard(ctx, 'album-choices'))])
    def home_album_choices(request: Request):
        return templates.TemplateResponse(request, "_partials/album_choices.html",
                                          album_context(ctx, views, now_fn()))

    def rotate_album_choices(previous=''):
        menu.rotate(now_fn(), previous)

    @router.post("/home/album-choices/refresh", dependencies=[Depends(guard(ctx, 'album-choices'))])
    def refresh_album_choices(request: Request, form=Depends(request_form)):
        rotate_album_choices(form.get('opportunity_id', ''))
        return home_album_choices(request)

    @router.post('/home/album-choices/tune', dependencies=[Depends(guard(ctx, 'album-choices'))])
    def tune_album_choices(request: Request, form=Depends(request_form)):
        try:
            reset = form.get('reset') == '1'
            values = {} if reset else {axis['key']: float(form[axis['key']]) / 100 for axis in album_tuning.AXES}
            menu.tune(now_fn(), values, form.getlist('genre'), reset=reset, previous=form.get('opportunity_id', ''))
        except (KeyError, ValueError, TypeError):
            return Response('Choose valid album controls', status_code=422)
        return home_album_choices(request)

    @router.get('/home/album-notes', dependencies=[Depends(guard(ctx, 'album-choices'))])
    def home_album_notes(request: Request, browse: str, opportunity: str = '', attempt: int = 0):
        context = menu.note(browse, now_fn(), opportunity, attempt)
        if context is None:
            return Response(status_code=404)
        return templates.TemplateResponse(request, '_partials/album_notes.html', context)

    @router.post("/home/listening-response", status_code=204)
    def listening_response(event: ChoiceResponse):
        if not commands.response(event.opportunity_id, event.option_id, event.kind, now_fn()):
            return Response(status_code=400)
        return Response(status_code=204)

    @router.get("/home/into-recently", dependencies=[Depends(guard(ctx, 'recent'))])
    def home_into_recently(request: Request):
        """Read worker-prepared stories; the request only rotates the already decorated pool."""
        return story_fragment(ctx, request)

    @router.post("/home/into-recently/impressions", status_code=204, dependencies=[Depends(guard(ctx, 'recent'))])
    def story_impressions(visible: StoryImpressions):
        commands.story_impressions(visible.subjects, now_fn())
        return Response(status_code=204)

    @router.get("/home/discovery", dependencies=[Depends(guard(ctx, 'discover'))])
    def home_discovery(request: Request):
        return templates.TemplateResponse(request, "_partials/home_discovery.html",
                                          views.discovery_context(now_fn()))

    @router.get('/home/revisit', dependencies=[Depends(guard(ctx, 'rediscover'))])
    def home_revisit(request: Request):
        now = now_fn()
        return templates.TemplateResponse(request, '_partials/rediscover.html',
            home_prepared.collections(store, views.prepared(now), now))

    @router.get("/home/discover", dependencies=[Depends(guard(ctx, 'discover'))])
    def home_discover(request: Request):
        # From the accumulating discovery pool: recency-biased, repeat-aware, filled in by the
        # background scan of every artist you're interested in (not a top-10 overwrite).
        now = now_fn()
        albums = views.discovery_context(now)["discovery_albums"]
        return templates.TemplateResponse(request, "_partials/discover.html",
                                          {"albums": albums,
                                           "building": not albums and views.busy(),
                                           "stale": bool(albums) and views.busy()})

    @router.get("/home/new-artists", dependencies=[Depends(guard(ctx, 'discover'))])
    def home_new_artists(request: Request):
        # From the accumulating new-artist pool: best taste-fit first, de-prioritizing recently-shown.
        now = now_fn()
        artists = views.discovery_context(now)["discovery_artists"]
        return templates.TemplateResponse(request, "_partials/new_artists.html",
                                          {"artists": artists,
                                           "building": not artists and views.busy(),
                                           "stale": bool(artists) and views.busy()})

    return router

