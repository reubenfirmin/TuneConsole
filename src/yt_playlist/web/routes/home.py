"""Home tab: the default landing page — Sync control, Take-Action triage, and For-You recs."""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Request

from yt_playlist import executor, rec_params, recommend
from yt_playlist.rec_dao import RecDao

# How many tracks each generated proto-playlist offers.
PROTO_SIZE = 12
_NOTES = {
    "wheelhouse": "Deeper into what you already love.",
    "explore": "Unplayed tracks from your own playlists — corners you've drifted from.",
    "fresh": "Tracks that aren't in your collection yet.",
    "comfort": "Your most-played favorites you haven't reached for lately.",
}


def _proto(lane, label, items, now):
    """Shape a recommendation lane into a dated, saveable proto-playlist card."""
    when = datetime.fromtimestamp(now).strftime("%B %-d %Y")   # e.g. "June 21 2026"
    return {"lane": lane, "label": label, "name": f"{label} - {when}",
            "note": _NOTES.get(lane, ""), "tracks": items[:PROTO_SIZE]}


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _feed_context(now, erode=True):
        # Landing keeps erosion (fresh daily rotation); the steer/stance re-rank passes erode=False so
        # the user sees their taste's true ranking and actually feels the change they just made.
        for_you = recommend.for_you(store, now, erode=erode)
        shown = {i.key for i in for_you}
        explore = [i for i in recommend.explore_for_you(store, now) if i.key not in shown]
        dao = RecDao(store)   # record what was shown so erosion can recycle stale items
        dao.record_impressions("for_you", [i.key for i in for_you if i.key], now)
        dao.record_impressions("explore", [i.key for i in explore if i.key], now)
        stance = store.get_setting("home_stance") or "exploit"
        wheel = _proto("wheelhouse", "More in your wheelhouse", for_you, now)
        catalog = _proto("explore", "From your catalog", explore, now) if explore else None
        generated = [catalog, wheel] if stance == "explore" and catalog else \
                    [wheel] + ([catalog] if catalog else [])
        # Comfort Listening: a fixed 4th card, kept out of the stance reordering. Dedup against the
        # taste-driven lanes above so a track never shows in two cards on the same page.
        feed_keys = {i.key for i in for_you} | {i.key for i in explore}
        comfort_items = [i for i in recommend.comfort_listening(store, now) if i.key not in feed_keys]
        comfort = _proto("comfort", "Comfort listening", comfort_items, now) if comfort_items else None
        return {"fingerprint": recommend.taste_fingerprint(store),
                "generated": generated, "comfort": comfort, "stance": stance}

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        return templates.TemplateResponse(request, "home.html", {
            "actions": recommend.take_action(store, now, ctx.auth_expired),
            "sync": recommend.sync_status(store, now),
            "muted_count": len(store.muted_artists()),   # transparency: what's being hidden
            "flash": request.query_params.get("flash"),
            **_feed_context(now),
        })

    @router.get("/home/feed")
    def home_feed(request: Request):
        return templates.TemplateResponse(request, "_partials/home_feed.html",
                                          _feed_context(now_fn(), erode=False))

    @router.post("/home/stance")
    async def home_stance(request: Request):
        stance = (await request.form()).get("stance", "exploit")
        store.set_setting("home_stance", "explore" if stance == "explore" else "exploit")
        return templates.TemplateResponse(request, "_partials/home_feed.html",
                                          _feed_context(now_fn(), erode=False))

    @router.post("/home/steer")
    async def home_steer(request: Request):
        # Drag a fingerprint bar -> set that genre/era weight and return the re-ranked feed in one
        # request (erode=False so the change is felt). Genre/era/artist use the [0,2] band.
        form = await request.form()
        axis, weight = form.get("axis"), form.get("weight")
        if axis and weight and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            store.set_weight(axis, float(weight), lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
        return templates.TemplateResponse(request, "_partials/home_feed.html",
                                          _feed_context(now_fn(), erode=False))

    @router.post("/home/generate")
    async def home_generate(request: Request):
        """Materialize a proto-playlist as a real YouTube playlist, auto-grouped 'Generated' so the
        rec engine ignores it until it's played. The card's surviving rows are posted as `tracks`."""
        form = await request.form()
        name = (form.get("name") or "").strip()
        try:
            tracks = json.loads(form.get("tracks") or "[]")
        except (ValueError, TypeError):
            tracks = []
        identity_id, client = next(iter((ctx.client_provider() or {}).items()), (None, None))
        result = {"name": name}
        if client is None or not tracks:
            result["error"] = "Couldn't create it — connect an account and keep at least one track."
        else:
            try:
                res = await asyncio.to_thread(
                    executor.create_generated_playlist, store, name, tracks, client, now_fn(), identity_id)
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
            except Exception:  # noqa: BLE001 - surface a friendly card, log the detail
                ctx.logger.exception("generate playlist %r failed", name)
                result["error"] = "YouTube returned an unexpected response."
        return templates.TemplateResponse(request, "_partials/generated_result.html", result)

    def _busy():
        return bool(ctx.rec_worker and ctx.rec_worker.busy)

    @router.get("/home/auto-playlists")
    def home_auto_playlists(request: Request):
        props = RecDao(store).get_proposals("auto_playlists")
        if props is None and not _busy():
            props = recommend.auto_playlists(store, k=40)   # first-load fallback
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
        proto = _proto("fresh", "Fresh songs", props, now_fn()) if props else None
        return templates.TemplateResponse(request, "_partials/fresh.html",
                                          {"proto": proto,
                                           "building": props is None and _busy(),
                                           "stale": props is not None and _busy()})

    @router.get("/home/new-artists")
    def home_new_artists(request: Request):
        props = RecDao(store).get_proposals("new_artists")
        return templates.TemplateResponse(request, "_partials/new_artists.html",
                                          {"artists": props or [],
                                           "building": props is None and _busy(),
                                           "stale": props is not None and _busy()})

    return router
