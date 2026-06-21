"""Home tab: the default landing page — Sync control, Take-Action triage, and For-You recs."""
from fastapi import APIRouter, Request

from yt_playlist import recommend
from yt_playlist.rec_dao import RecDao


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        for_you = recommend.for_you(store, now)
        explore = recommend.explore_for_you(store, now)
        dao = RecDao(store)   # record what was shown so erosion can recycle stale items
        dao.record_impressions("for_you", [i.key for i in for_you if i.key], now)
        dao.record_impressions("explore", [i.key for i in explore if i.key], now)
        return templates.TemplateResponse(request, "home.html", {
            "actions": recommend.take_action(store, now, ctx.auth_expired),
            "sync": recommend.sync_status(store, now),
            "for_you": for_you,
            "explore": explore,
            "muted_count": len(store.muted_artists()),   # transparency: what's being hidden
            "flash": request.query_params.get("flash"),
        })

    def _busy():
        return bool(ctx.rec_worker and ctx.rec_worker.busy)

    @router.get("/home/auto-playlists")
    def home_auto_playlists(request: Request):
        props = RecDao(store).get_proposals("auto_playlists")
        if props is None and not _busy():
            props = recommend.auto_playlists(store, k=24)   # first-load fallback
        return templates.TemplateResponse(request, "_partials/auto_playlists.html",
                                          {"proposals": props or [],
                                           "building": props is None and _busy(),
                                           "stale": props is not None and _busy()})

    @router.get("/home/discover")
    def home_discover(request: Request):
        # outward discovery is materialized by the background worker (network) — never per-request
        props = RecDao(store).get_proposals("discover")
        return templates.TemplateResponse(request, "_partials/discover.html",
                                          {"albums": props or [],
                                           "building": props is None and _busy(),
                                           "stale": props is not None and _busy()})

    @router.get("/home/fresh")
    def home_fresh(request: Request):
        props = RecDao(store).get_proposals("fresh_songs")
        return templates.TemplateResponse(request, "_partials/fresh.html",
                                          {"songs": props or [],
                                           "building": props is None and _busy()})

    return router
