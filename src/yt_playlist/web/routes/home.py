"""Home HTTP resources render a complete detached page model."""
from fastapi import APIRouter, Request

from yt_playlist.library.home_query_composition import open_home
from yt_playlist.web.home_timing import opening
from yt_playlist.web.radio_projection import home_view as radio_home_view
from yt_playlist.web.routes import home_artist_start, home_board, home_discovery, home_generation, home_growth, home_onboarding, home_tuning, home_tv
from yt_playlist.web.routes.home_views import HomeViews, PoolJobs, card_view
from yt_playlist.web.update_labels import update_instruction
from yt_playlist.web.artist_links import radio_navigation


def page_view(model):
    update = model['backend_update']
    if update is not None:
        command, link = update_instruction(update['kind'])
        update = {**update, 'command': command, 'link': link}
    return {**card_view(model), 'backend_update': update,
            'radio_bootstrap': radio_home_view(model['radio_bootstrap'])}


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
    def home_page(request: Request):
        try:
            attempt = int(request.query_params.get('prepare_attempt', '0'))
        except ValueError:
            attempt = 0
        with opening() as timing:
            model = open_home(ctx, ctx.now_fn(), jobs=PoolJobs(ctx), schedule=views.schedule, timing=timing,
                launch=request.query_params.get('radio_playlist'), flash=request.query_params.get('flash'),
                toast=request.query_params.get('toast'), attempt=attempt)
            request.state.account_growth = model['growth']
            bootstrap = model['radio_bootstrap']
            model = {**model, 'radio_bootstrap': {**bootstrap,
                'state': radio_navigation(bootstrap['state'], ctx.store.artists)}}
            with timing.measure('template'):
                return ctx.templates.TemplateResponse(request, 'home.html', page_view(model))

    @router.get('/privacy')
    def privacy(request: Request):
        return ctx.templates.TemplateResponse(request, 'privacy.html', {})

    @router.get('/home/generating')
    def home_generating(request: Request):
        return ctx.templates.TemplateResponse(request, 'generating.html', {})

    return router
