"""Home page composition; feature routes own their fragments and mutations."""
from fastapi import APIRouter, Request

from yt_playlist.core import updatecheck
from yt_playlist.rec import actions, home_prepared, onboarding, rec_params
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.rec.trend_statistics import last_complete_month
from yt_playlist.web.routes import home_board, home_discovery, home_generation, home_onboarding, home_tuning
from yt_playlist.web.routes.home_onboarding import lastfm_nudge_due, takeout_nag_due
from yt_playlist.web.routes.home_views import HomeViews
from yt_playlist.web.spotify_import import current_job as current_spotify_import_job

ROTATING_CARDS = ("wheelhouse", "explore", "comfort", "fresh", "new_artists", "discover",
                  "rediscover", "into_recently", "cards")
MENU_MAX_AGE_S = 86400


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates
    views = HomeViews(ctx)
    router.include_router(home_board.build(ctx, views))
    router.include_router(home_tuning.build(ctx, views))
    router.include_router(home_discovery.build(ctx, views))
    router.include_router(home_onboarding.build(ctx))
    router.include_router(home_generation.build(ctx))

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        dao = store.rec_ledger
        for card in ROTATING_CARDS:   # one tick per genuine Home visit -> per-card rotation advances
            if card == "cards":
                started = store.modes.epoch_started_at(_epoch(store, card))
                if started is not None and now - started >= MENU_MAX_AGE_S:
                    dao.refresh_card(card, max(1, rec_params.get_param(store, "erosion_view_cap")), now)
                    continue
            dao.bump_card_view(card, now)
        backend_update = updatecheck.update_nudge(store)
        # Monthly recap ("Your <Month>"): once a calendar month is baked into the rollup, surface it as
        # a nag card (gated on a completed first sync) that links into the full-screen reel. Shown
        # until viewed or dismissed for that month; next month's recap re-shows. Expire a cached story
        # at the next rollover even if the worker has not rebuilt yet, or recent listening data is missing.
        _story = (store.rec_proposals.get_proposals("trend_rollups") or {}).get("story")
        recap = ({"month": _story["month"], "month_name": _story["month_name"]}
                 if (_story and _story.get("month") == last_complete_month(now)
                     and store.settings.get_setting("last_sync_at") is not None
                     and store.settings.get_setting("recap_viewed_month") != _story.get("month")
                     and store.settings.get_setting("recap_dismissed_month") != _story.get("month"))
                 else None)
        spotify_library_job = current_spotify_import_job(ctx)
        if spotify_library_job and spotify_library_job.get("dismissed"):
            spotify_library_job = None
        radio_bootstrap = ctx.radio.bootstrap()
        launch_id = request.query_params.get("radio_playlist")
        if launch_id is not None:
            source = store.playlists.get_playlist(int(launch_id)) if launch_id.isdecimal() and len(launch_id) < 16 else None
            if source is not None:
                radio_bootstrap["launchPlaylist"] = {"id": source.id, "title": source.title,
                    "tracks": [track for track in store.charts.playlist_tracks_detail(source.id)
                               if track.get("video_id") and track.get("available") != 0][:8]}
        radio_state = radio_bootstrap["state"]
        radio_tracks = (store.charts.video_tracks_detail([t["video_id"] for t in radio_state["history"]])
                        if radio_state.get("active") and radio_state.get("local_id") else [])
        collections = home_prepared.collections(store, views.prepared(now, request), now)
        views.queue_artist_images(collections, now)
        feed = views.feed_context(request, now)
        # Imported likes, dismissal and fitted modes are not complete listening offers.
        # Returning Home during setup must retain the same useful first-run screen.
        getting_started = (not feed["protos"] and
                           (onboarding.initial_setup_pending(store, now) or onboarding.onboarding_active(store, now)))
        return templates.TemplateResponse(request, "home.html", {
            "home_default_panel": {"albums": "album-choices", "mixes": "directions", "radio": "radio",
                "both": store.settings.get_setting("home_last_panel") or "directions"}.get(
                    store.settings.get_setting("home_format"), "directions"),
            "radio_bootstrap": radio_bootstrap,
            "radio_tracks": radio_tracks,
            "actions": actions.take_action(store, now, ctx.auth_expired),
            "sync": actions.sync_status(store, now),
            "muted_count": len(store.rec_model.muted_artists()),   # transparency: what's being hidden
            **collections,
            "flash": request.query_params.get("flash"),
            "toast": request.query_params.get("toast"),   # transient success (e.g. re-auth w/ auto-sync)
            # Sparsity nudge: when metadata is genre-thin and no Last.fm key is set, point the user
            # at adding one (persists once dismissed). The auto-enrich worker handles the rest, so
            # there is no manual-enrichment nag anymore.
            # Welcome nag shown once the first sync lands (dismissable, persists): sets expectations
            # that the model keeps learning over the next week.
            "show_intro": (store.settings.get_setting("last_sync_at") is not None
                           and store.settings.get_setting("intro_dismissed") != "1"),
            "show_lastfm_nudge": lastfm_nudge_due(store, now),
            # Optional history import follows onboarding and retires once an import lands.
            "show_takeout_nag": takeout_nag_due(store, now),
            "backend_update": backend_update,
            "show_backend_update": backend_update is not None,
            # Once the library has synced and the user still has only the default identity, offer the
            # multi-identity merge (dismissable, persists). Skipped as soon as they add a second one.
            "show_identities_nudge": (store.settings.get_setting("last_sync_at") is not None
                                      and len(store.identities.get_identities()) <= 1
                                      and store.settings.get_setting("identities_nudge_dismissed") != "1"),
            "onboarding": getting_started,
            "onboard_library": onboarding.library_size(store) > 0,
            "cleanup_count": onboarding.cleanup_count(store),   # CACHED read, never the O(n^2) scan
            "recap": recap,
            # Persisted background-import state. Completion stays in Needs attention with its full
            # report until explicitly dismissed, including across app/browser restarts.
            "spotify_library_job": spotify_library_job,
            "discovery_busy": views.busy(),
            **feed,
        })

    @router.get("/privacy")
    def privacy(request: Request):
        return templates.TemplateResponse(request, "privacy.html", {})

    @router.get("/home/generating")
    def home_generating(request: Request):
        """Spinner interstitial shown in the popup we open on the 'Save & play' click, until the save
        round-trip returns and the home tab redirects this window to the new YouTube playlist."""
        return templates.TemplateResponse(request, "generating.html", {})

    return router
