"""Home onboarding samples, arrival status and optional reminder lifecycle."""
import time

from fastapi import APIRouter, Request, Response

from yt_playlist.rec import onboarding

LASTFM_NUDGE_SNOOZE_S = 30 * 86400
TAKEOUT_NAG_SNOOZE_S = 90 * 86400


def lastfm_nudge_due(store, now=None):
    return onboarding.lastfm_nudge_due(store, time.time() if now is None else now)


def takeout_nag_due(store, now=None):
    return onboarding.takeout_nag_due(store, time.time() if now is None else now)


def sample_fragment(ctx, request, kind):
    proto = onboarding.sample(ctx.store, kind, ctx.client_provider, ctx.now_fn())
    return ctx.templates.TemplateResponse(request, "_partials/onboard_playlist.html", {"proto": proto})


def build(ctx):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.post("/onboard/lastfm/dismiss")
    def dismiss_lastfm_nudge():
        onboarding.dismiss_lastfm_nudge(store, now_fn())
        return Response(status_code=200)

    @router.post("/onboard/takeout/dismiss")
    def dismiss_takeout_nag():
        onboarding.dismiss_takeout_nag(store, now_fn())
        return Response(status_code=200)

    @router.post("/onboard/identities/dismiss")
    def dismiss_identities_nudge():
        onboarding.dismiss_identities_nudge(store)
        return Response(status_code=200)

    @router.post("/onboard/intro/dismiss")
    def dismiss_intro_nudge():
        onboarding.dismiss_intro_nudge(store)
        return Response(status_code=200)

    @router.post("/onboard/welcome/dismiss")
    def dismiss_welcome():
        store.settings.set_setting('onboard_welcome_dismissed', '1')
        return Response(status_code=204)

    @router.post("/onboard/player-tour/dismiss")
    def dismiss_player_tour():
        store.settings.set_setting('onboard_player_tour_dismissed', '1')
        return Response(status_code=204)

    @router.post("/onboard/recap/dismiss")
    def dismiss_recap_nudge(month: str = ""):
        onboarding.dismiss_recap_nudge(store, month)
        return Response(status_code=200)

    @router.post("/onboard/update/dismiss")
    def dismiss_update_nudge(v: str = ""):
        onboarding.dismiss_update_nudge(store, v)
        return Response(status_code=200)

    @router.get("/home/onboard/radio")
    def home_onboard_radio(request: Request):
        return sample_fragment(ctx, request, "onboard_radio")

    @router.get("/home/onboard/library")
    def home_onboard_library(request: Request):
        return sample_fragment(ctx, request, "onboard_library")

    @router.get("/home/onboard/status")
    def home_onboard_status():
        return onboarding.status(store, now_fn(), ctx.jobs, ctx.rec_worker)

    @router.get('/home/onboard/progress')
    def home_onboard_progress():
        return onboarding.import_progress(store, ctx.jobs, ctx.rec_worker)

    @router.get("/home/onboard/start")
    def home_onboard_start(request: Request):
        starter = onboarding.starter(store, ctx.radio, now_fn())
        if starter['kind'] == 'artists':
            return templates.TemplateResponse(request, 'artist_start.html', starter)
        if starter['kind'] == 'albums':
            return templates.TemplateResponse(request, '_partials/onboarding_album.html', starter)
        if starter['kind'] == 'radio':
            return templates.TemplateResponse(request, '_partials/onboarding_radio.html', starter)
        return templates.TemplateResponse(request, '_partials/onboarding_start.html', starter)

    @router.post("/onboard/done")
    def onboard_done():
        onboarding.complete(store)
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    return router
