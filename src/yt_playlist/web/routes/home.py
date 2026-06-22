"""Home tab: the default landing page — Sync control, Take-Action triage, and For-You recs."""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Request

from yt_playlist import executor, rec_params, recommend
from yt_playlist.rec_dao import RecDao

# How many tracks each generated proto-playlist offers.
PROTO_SIZE = 12
ROTATION_POOL = PROTO_SIZE * 5      # fetch this deep so each epoch's random slice is genuinely fresh
ARTISTS_PER_CARD = 10              # new-artist tiles fetched per epoch (grid caps 5 cols, clamps to 2 rows)
ALBUMS_PER_CARD = 15               # discover album tiles fetched per epoch (grid caps 5 cols, clamps to 3 rows)
# Home cards that rotate. Each holds its content for erosion_view_cap real Home visits, then advances
# to a fresh epoch. They tick together (once per visit, in GET /) but each rotates its OWN pool at its
# own size — so the small new-artist pool cycles through faster than the deep playlist pool.
ROTATING_CARDS = ("wheelhouse", "explore", "comfort", "fresh", "new_artists", "discover")
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


def _carded(store, lane, label, items, now):
    """A proto-card built from a rolled recipe: roll the theme (seeded by the card's rotation epoch so
    it's stable across steer/stance previews, like the rotation), focus the items on it, and attach
    the recipe so a Save persists exactly how this mix was made."""
    recipe = recommend.roll_recipe(store, lane, seed=_epoch(store, lane))
    p = _proto(lane, label, recommend.theme_filter(store, items, recipe.get("facets", {})), now)
    p["recipe"] = recipe
    return p


def _epoch(store, card):
    """The card's current rotation epoch: it holds content for erosion_view_cap views, then advances.
    Read-only — the view tick happens in GET / so previews/re-renders don't churn the cards."""
    cap = max(1, rec_params.get_param(store, "erosion_view_cap"))
    return max(0, RecDao(store).card_views(card) - 1) // cap


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _feed_context(now):
        # Each list card shows a stable random slice of its ranked pool, reseeded once its rotation
        # epoch advances (every erosion_view_cap real Home visits). The epoch is read here, never
        # ticked — the tick happens only in GET /, so steer/stance previews re-render the same slice
        # and tuning your taste model never churns the cards.
        fy_pool = recommend.for_you(store, now, limit=ROTATION_POOL)
        fy_keys = {i.key for i in fy_pool}
        ex_pool = [i for i in recommend.explore_for_you(store, now, limit=ROTATION_POOL)
                   if i.key not in fy_keys]
        wheel_items = recommend.rotate_sample(fy_pool, PROTO_SIZE, _epoch(store, "wheelhouse"))
        catalog_items = recommend.rotate_sample(ex_pool, PROTO_SIZE, _epoch(store, "explore"))
        # Comfort Listening: a fixed 4th card, kept out of the stance reordering. Dedup against what
        # the taste lanes are *currently showing* so a track never appears twice on the page.
        shown = {i.key for i in wheel_items} | {i.key for i in catalog_items}
        cf_pool = [i for i in recommend.comfort_listening(store, now, limit=ROTATION_POOL)
                   if i.key not in shown]
        comfort_items = recommend.rotate_sample(cf_pool, PROTO_SIZE, _epoch(store, "comfort"))

        stance = store.get_setting("home_stance") or "exploit"
        wheel = _carded(store, "wheelhouse", "More in your wheelhouse", wheel_items, now)
        catalog = _carded(store, "explore", "From your catalog", catalog_items, now) if catalog_items else None
        generated = [catalog, wheel] if stance == "explore" and catalog else \
                    [wheel] + ([catalog] if catalog else [])
        comfort = _carded(store, "comfort", "Comfort listening", comfort_items, now) if comfort_items else None
        return {"fingerprint": recommend.taste_fingerprint(store),
                "generated": generated, "comfort": comfort, "stance": stance}

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        dao = RecDao(store)
        for card in ROTATING_CARDS:   # one tick per genuine Home visit -> per-card rotation advances
            dao.bump_card_view(card, now)
        return templates.TemplateResponse(request, "home.html", {
            "actions": recommend.take_action(store, now, ctx.auth_expired),
            "sync": recommend.sync_status(store, now),
            "muted_count": len(store.muted_artists()),   # transparency: what's being hidden
            "rediscover": recommend.rediscover_playlists(store, now),
            "flash": request.query_params.get("flash"),
            **_feed_context(now),
        })

    @router.get("/home/feed")
    def home_feed(request: Request):
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

    @router.post("/home/stance")
    async def home_stance(request: Request):
        stance = (await request.form()).get("stance", "exploit")
        store.set_setting("home_stance", "explore" if stance == "explore" else "exploit")
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

    @router.post("/home/steer")
    async def home_steer(request: Request):
        # Drag a fingerprint bar -> set that genre/era weight and return the re-ranked feed in one
        # request. Re-ranks in place (no rotation tick), so the change is felt. [0,2] band.
        form = await request.form()
        axis, weight = form.get("axis"), form.get("weight")
        if axis and weight and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            store.set_weight(axis, float(weight), lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

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
        try:
            recipe = json.loads(form.get("recipe") or "null")
        except (ValueError, TypeError):
            recipe = None
        identity_id, client = next(iter((ctx.client_provider() or {}).items()), (None, None))
        result = {"name": name}
        if client is None or not tracks:
            result["error"] = "Couldn't create it — connect an account and keep at least one track."
        else:
            try:
                res = await asyncio.to_thread(
                    executor.create_generated_playlist, store, name, tracks, client, now_fn(),
                    identity_id, recipe=recipe)
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
        # From the accumulating discovery pool: recency-biased, repeat-aware, filled in by the
        # background scan of every artist you're interested in (not a top-10 overwrite).
        from yt_playlist import discover
        albums = discover.pick_discovered_albums(store, ALBUMS_PER_CARD, now_fn())
        return templates.TemplateResponse(request, "_partials/discover.html",
                                          {"albums": albums,
                                           "building": not albums and _busy(),
                                           "stale": bool(albums) and _busy()})

    @router.get("/home/fresh")
    def home_fresh(request: Request):
        # List card: a fresh random slice each epoch, like the other proto-playlists.
        props = RecDao(store).get_proposals("fresh_songs")
        items = recommend.rotate_sample(props or [], PROTO_SIZE, _epoch(store, "fresh"))
        proto = _carded(store, "fresh", "Fresh songs", items, now_fn()) if items else None
        return templates.TemplateResponse(request, "_partials/fresh.html",
                                          {"proto": proto,
                                           "building": props is None and _busy(),
                                           "stale": props is not None and _busy()})

    @router.get("/home/new-artists")
    def home_new_artists(request: Request):
        # From the accumulating new-artist pool: best taste-fit first, de-prioritizing recently-shown.
        from yt_playlist import discover
        artists = discover.pick_discovered_artists(store, ARTISTS_PER_CARD, now_fn())
        return templates.TemplateResponse(request, "_partials/new_artists.html",
                                          {"artists": artists,
                                           "building": not artists and _busy(),
                                           "stale": bool(artists) and _busy()})

    return router
