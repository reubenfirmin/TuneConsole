"""Home onboarding samples, arrival status and optional reminder lifecycle."""
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from yt_playlist.core.accounts import main_client
from yt_playlist.providers import lastfm
from yt_playlist.rec import home_cards, home_preferences, onboarding
from yt_playlist.rec.home_cards import _proto

LASTFM_NUDGE_SNOOZE_S = 30 * 86400
TAKEOUT_NAG_SNOOZE_S = 90 * 86400


def lastfm_nudge_due(store, now=None):
    if lastfm.available(store):
        return False
    dismissed_at = store.settings.get_setting("lastfm_nudge_dismissed_at")
    if dismissed_at is not None:
        try:
            if (now if now is not None else time.time()) - float(dismissed_at) < LASTFM_NUDGE_SNOOZE_S:
                return False
        except (TypeError, ValueError):
            pass
    return True


def takeout_nag_due(store, now=None):
    if not onboarding._has_synced(store) or onboarding.onboarding_active(store, now):
        return False
    if store.settings.get_setting("takeout_imported_at") is not None:
        return False
    dismissed_at = store.settings.get_setting("takeout_nag_dismissed_at")
    if dismissed_at is not None:
        try:
            if (now if now is not None else time.time()) - float(dismissed_at) < TAKEOUT_NAG_SNOOZE_S:
                return False
        except (TypeError, ValueError):
            pass
    return True


def sample_fragment(ctx, request, kind):
    now = ctx.now_fn()
    if kind == "onboard_radio":
        tracks = onboarding.radio_sample(ctx.store, main_client(ctx.store, ctx.client_provider()),
                                          now, n=home_cards.PROTO_SIZE)
    else:
        tracks = onboarding.library_sample(ctx.store, n=home_cards.PROTO_SIZE)
    proto = _proto(ctx.store, kind, "Something to listen to", tracks, now) if tracks else None
    return ctx.templates.TemplateResponse(request, "_partials/onboard_playlist.html", {"proto": proto})


def build(ctx):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.post('/home/onboard/format')
    async def choose_listening_format(request: Request):
        preference = (await request.form()).get('format')
        if preference not in home_preferences.PANELS:
            return Response('Choose albums, mixes or radio.', status_code=422)
        store.settings.set_setting('onboard_listening_format', preference)
        store.settings.set_setting('onboard_format_pending', '0')
        return RedirectResponse('/', status_code=303)

    @router.post("/onboard/lastfm/dismiss")
    def dismiss_lastfm_nudge():
        store.settings.set_setting("lastfm_nudge_dismissed_at", str(now_fn()))
        return Response(status_code=200)

    @router.post("/onboard/takeout/dismiss")
    def dismiss_takeout_nag():
        store.settings.set_setting("takeout_nag_dismissed_at", str(now_fn()))
        return Response(status_code=200)

    @router.post("/onboard/identities/dismiss")
    def dismiss_identities_nudge():
        store.settings.set_setting("identities_nudge_dismissed", "1")
        return Response(status_code=200)

    @router.post("/onboard/intro/dismiss")
    def dismiss_intro_nudge():
        store.settings.set_setting("intro_dismissed", "1")
        return Response(status_code=200)

    @router.post("/onboard/recap/dismiss")
    def dismiss_recap_nudge(month: str = ""):
        if month:
            store.settings.set_setting("recap_dismissed_month", month)
        return Response(status_code=200)

    @router.post("/onboard/update/dismiss")
    def dismiss_update_nudge(v: str = ""):
        dismissed = v or store.settings.get_setting("latest_version_seen")
        if dismissed:
            store.settings.set_setting("backend_update_dismissed_version", dismissed)
        return Response(status_code=200)

    @router.get("/home/onboard/radio")
    def home_onboard_radio(request: Request):
        return sample_fragment(ctx, request, "onboard_radio")

    @router.get("/home/onboard/library")
    def home_onboard_library(request: Request):
        return sample_fragment(ctx, request, "onboard_library")

    @router.get("/home/onboard/status")
    def home_onboard_status():
        status = onboarding.arrival_status(store, now_fn())
        status['setup']['listening_format'] = home_preferences.default_format(store)
        job = ctx.jobs.find_active_library()
        events = job.events.since(0)[1] if job else []
        status["setup"]["running"] = bool(job)
        status["setup"]["step"] = next((event.get("text", "") for _, event in reversed(events)
                                        if event.get("text")), "")
        status["setup"]["training"] = bool(ctx.rec_worker and ctx.rec_worker.busy)
        progress = next((event for _, event in reversed(events) if event.get("stage")), {})
        status["setup"].update(activity=progress.get("stage"), current=progress.get("i", progress.get("index")),
                                target=progress.get("total", progress.get("count")))
        return status

    @router.get("/home/onboard/start")
    def home_onboard_start(request: Request):
        tracks = onboarding.library_sample(store, n=home_cards.PROTO_SIZE)
        proto = _proto(store, "onboard_library", "Something to listen to", tracks, now_fn()) if tracks else None
        return templates.TemplateResponse(request, "_partials/onboarding_start.html", {"proto": proto})

    @router.post("/onboard/done")
    def onboard_done():
        store.settings.set_setting("onboard_dismissed", "1")
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    return router
