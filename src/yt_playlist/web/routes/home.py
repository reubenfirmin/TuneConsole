"""Home HTTP resources render a complete detached page model."""
from fastapi import APIRouter, BackgroundTasks, Request, Response
from jinja2.meta import find_referenced_templates

from yt_playlist.controllers.home_query import PANELS, PanelLocked
from yt_playlist.library.home_query_composition import open_home
from yt_playlist.web.home_timing import opening
from yt_playlist.web.radio_projection import home_view as radio_home_view
from yt_playlist.web.routes import home_artist_start, home_board, home_discovery, home_generation, home_growth, home_onboarding, home_tuning, home_tv
from yt_playlist.web.routes.home_views import HomeViews, PoolJobs, after_response, card_view, prepare_attempt
from yt_playlist.web.update_labels import update_instruction
from yt_playlist.web.artist_links import navigation, radio_navigation


def preload_templates(environment):
    """Compile Home and its static includes before the server accepts a visit."""
    pending = ['home.html']
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        environment.get_template(name)
        source, _, _ = environment.loader.get_source(environment, name)
        pending.extend(reference for reference in find_referenced_templates(environment.parse(source)) if reference)


def page_view(model, artists):
    update = model['backend_update']
    if update is not None:
        command, link = update_instruction(update['kind'])
        update = {**update, 'command': command, 'link': link}
    bootstrap = model['radio_bootstrap']
    if bootstrap:
        bootstrap = radio_home_view({**bootstrap, 'state': radio_navigation(bootstrap['state'], artists)})
    return {**card_view(model), 'backend_update': update, 'artist_links': navigation(artists),
            'radio_bootstrap': bootstrap}


def build(ctx) -> APIRouter:
    router = APIRouter()
    views = HomeViews(ctx)
    router.include_router(home_board.build(ctx, views))
    router.include_router(home_tuning.build(ctx, views))
    router.include_router(home_discovery.build(ctx, views))
    router.include_router(home_onboarding.build(ctx))
    router.include_router(home_artist_start.build(ctx))
    router.include_router(home_growth.build(ctx))
    router.include_router(home_generation.build(ctx))
    router.include_router(home_tv.build(ctx))

    @router.get('/')
    def home_page(request: Request, background: BackgroundTasks, panel: str | None = None, fragment: bool = False):
        if fragment and panel not in PANELS:
            return Response(status_code=404)

        def defer(label, operation, *args):
            background.add_task(after_response, ctx, label, operation, args)

        try:
            with opening() as timing:
                with open_home(
                    ctx, ctx.now_fn(), jobs=PoolJobs(ctx), schedule=views.schedule, timing=timing, defer=defer,
                    requested=panel if panel in PANELS else None, fragment=fragment,
                    launch=request.query_params.get('radio_playlist'), flash=request.query_params.get('flash'),
                    toast=request.query_params.get('toast'), attempt=prepare_attempt(request),
                ) as (model, reader):
                    request.state.account_growth = model['growth']
                    context = {**page_view(model, reader.artists), 'home_fragment': fragment}
                    template = '_partials/home_panel.html' if fragment else 'home.html'
                    with timing.measure('template'):
                        request.state.home_shell = reader, defer
                        try:
                            return ctx.templates.TemplateResponse(request, template, context, background=background)
                        finally:
                            del request.state.home_shell
        except PanelLocked:
            return Response(status_code=409)

    @router.get('/privacy')
    def privacy(request: Request):
        return ctx.templates.TemplateResponse(request, 'privacy.html', {})

    @router.get('/home/generating')
    def home_generating(request: Request):
        return ctx.templates.TemplateResponse(request, 'generating.html', {})

    return router
