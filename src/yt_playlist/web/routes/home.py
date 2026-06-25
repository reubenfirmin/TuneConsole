"""Home tab: the default landing page — Sync control, Take-Action triage, and For-You recs."""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from yt_playlist.library import executor
from yt_playlist.rec import arc_energy, genre_map, journeys, rec_params, recommend
from yt_playlist.rec.rec_dao import RecDao

# How many tracks each generated proto-playlist offers.
PROTO_SIZE = 12
ROTATION_POOL = PROTO_SIZE * 5      # fetch this deep so each epoch's random slice is genuinely fresh
ARTISTS_PER_CARD = 10              # new-artist tiles fetched per epoch (grid caps 5 cols, clamps to 2 rows)
ALBUMS_PER_CARD = 15               # discover album tiles fetched per epoch (grid caps 5 cols, clamps to 3 rows)
# Home cards that rotate. Each holds its content for erosion_view_cap real Home visits, then advances
# to a fresh epoch. They tick together (once per visit, in GET /) but each rotates its OWN pool at its
# own size — so the small new-artist pool cycles through faster than the deep playlist pool.
ROTATING_CARDS = ("wheelhouse", "explore", "comfort", "fresh", "new_artists", "discover", "rediscover")
_NOTES = {
    "wheelhouse": "Deeper into what you already love.",
    "explore": "Unplayed corners of your own library.",
    "fresh": "Songs you don't own yet.",
    "comfort": "Old favorites you haven't played lately.",
}


def _proto(lane, label, items, now):
    """Shape a recommendation lane into a dated, saveable proto-playlist card."""
    when = datetime.fromtimestamp(now).strftime("%B %-d %Y")   # e.g. "June 21 2026"
    return {"lane": lane, "label": label, "name": f"{label} - {when}",
            "note": _NOTES.get(lane, ""), "tracks": items[:PROTO_SIZE]}


def _carded(store, lane, label, items, now):
    """A proto-card built from a rolled recipe: roll the theme + journey (seeded by the card's
    rotation epoch so it's stable across steer/stance previews), focus the items on the theme, then
    order them by the rolled JOURNEY (energy arc, eras, deep dive…) with genres attached. Ordering
    happens here — not at save — so the preview IS the playlist (WYSIWYG): a Save keeps the rows you
    didn't prune, in the order you see. Recipes predating journeys fall back to 'shuffle'."""
    recipe = recommend.roll_recipe(store, lane, seed=_epoch(store, lane), now=now)
    items = recommend.theme_filter(store, items, recipe.get("facets", {}))
    items = recommend.attach_genres(store, items)
    # roll_recipe forces Fresh to journey="shuffle" (unowned proposals have no plays/recency signal),
    # so the stored recipe and this preview ordering agree. Owned lanes order by their rolled journey.
    journey = recipe.get("journey", "shuffle")
    _f = recommend._field
    keys = [_f(it, "key") or "" for it in items]
    dao = RecDao(store)
    decades, lastp, plays = dao.track_decades(keys), dao.track_last_played(keys), store.play_counts()
    genres = {(_f(it, "key") or ""): (_f(it, "genre") or "") for it in items}
    arc = arc_energy.arc_energies(keys, genres, dao.track_audio_features())   # real-audio energy (#37)

    def feat(it):
        k, g = _f(it, "key") or "", _f(it, "genre") or ""
        return {"artist": _f(it, "artist") or "", "genre": g, "energy": arc.get(k, genre_map.energy(g)),
                "decade": decades.get(k), "plays": plays.get(k, 0), "recency": lastp.get(k, 0.0)}

    seed = recipe.get("dj", {}).get("seed", 0)
    items = journeys.journey_order(items, journey, seed, feat)
    p = _proto(lane, label, items, now)
    p["recipe"] = recipe
    p["refreshable"] = True
    return p


# Labels for the four refreshable Home cards, by their internal lane name.
_CARD_LABELS = {"wheelhouse": "More in your wheelhouse", "explore": "From your catalog",
                "comfort": "Comfort listening", "fresh": "Fresh songs"}


def _one_card(store, card, now):
    """Build a single Home card's proto (its pool, rotated at the card's CURRENT epoch). Used by the
    per-card Refresh route after the rotation has been advanced. Returns None if the card is empty."""
    if card == "wheelhouse":
        items = recommend.rotate_sample(recommend.for_you(store, now, limit=ROTATION_POOL),
                                        PROTO_SIZE, _epoch(store, "wheelhouse"))
    elif card == "explore":
        fy = {i.key for i in recommend.for_you(store, now, limit=ROTATION_POOL)}
        pool = [i for i in recommend.explore_for_you(store, now, limit=ROTATION_POOL) if i.key not in fy]
        items = recommend.rotate_sample(pool, PROTO_SIZE, _epoch(store, "explore"))
    elif card == "comfort":
        items = recommend.rotate_sample(recommend.comfort_listening(store, now, limit=ROTATION_POOL),
                                        PROTO_SIZE, _epoch(store, "comfort"))
    elif card == "fresh":
        props = RecDao(store).get_proposals("fresh_songs")
        items = recommend.rotate_sample(props or [], PROTO_SIZE, _epoch(store, "fresh"))
    else:
        return None
    return _carded(store, card, _CARD_LABELS[card], items, now) if items else None


def _epoch(store, card):
    """The card's current rotation epoch: it holds content for erosion_view_cap views, then advances.
    Read-only — the view tick happens in GET / so previews/re-renders don't churn the cards."""
    cap = max(1, rec_params.get_param(store, "erosion_view_cap"))
    return max(0, RecDao(store).card_views(card) - 1) // cap


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _feed_context(now):
        # Bake any held-day slider exposure into the graduation ledger (once per UTC day per axis).
        recommend.graduate_slider_exposure(store, now)
        # Each list card shows a stable random slice of its ranked pool, reseeded once its rotation
        # epoch advances (every erosion_view_cap real Home visits). The epoch is read here, never
        # ticked — the tick happens only in GET /, so steer/stance previews re-render the same slice
        # and tuning your taste model never churns the cards.
        # HOME CARD MAP (heading <- function): Wheelhouse <- for_you, Catalog <- explore_for_you,
        # Comfort <- comfort_listening, Fresh <- fresh_songs. Catalog ('ex_pool') is DEDUPED against
        # the Wheelhouse pool below, so the two cards never show the same track — but note both rank
        # off the same taste×transient score, so Catalog is currently "Wheelhouse's leftovers by
        # under-played artists" rather than a distinct novelty signal (a known differentiation gap).
        fy_pool = recommend.for_you(store, now, limit=ROTATION_POOL)            # Wheelhouse pool
        fy_keys = {i.key for i in fy_pool}
        ex_pool = [i for i in recommend.explore_for_you(store, now, limit=ROTATION_POOL)  # Catalog pool
                   if i.key not in fy_keys]                                     # deduped vs Wheelhouse
        wheel_items = recommend.rotate_sample(fy_pool, PROTO_SIZE, _epoch(store, "wheelhouse"))
        catalog_items = recommend.rotate_sample(ex_pool, PROTO_SIZE, _epoch(store, "explore"))
        # Comfort Listening: a fixed 4th card. Dedup against what the taste lanes are *currently
        # showing* so a track never appears twice on the page.
        shown = {i.key for i in wheel_items} | {i.key for i in catalog_items}
        cf_pool = [i for i in recommend.comfort_listening(store, now, limit=ROTATION_POOL)
                   if i.key not in shown]
        comfort_items = recommend.rotate_sample(cf_pool, PROTO_SIZE, _epoch(store, "comfort"))

        # Fixed order: wheelhouse first, then catalog (the old explore/exploit toggle that reordered
        # these was removed in #7 — breadth steering is the real focused<->eclectic control now).
        wheel = _carded(store, "wheelhouse", "More in your wheelhouse", wheel_items, now)
        catalog = _carded(store, "explore", "From your catalog", catalog_items, now) if catalog_items else None
        generated = [wheel] + ([catalog] if catalog else [])
        comfort = _carded(store, "comfort", "Comfort listening", comfort_items, now) if comfort_items else None
        return {"fingerprint": recommend.taste_fingerprint(store),
                "generated": generated, "comfort": comfort}

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        dao = RecDao(store)
        for card in ROTATING_CARDS:   # one tick per genuine Home visit -> per-card rotation advances
            dao.bump_card_view(card, now)
        return templates.TemplateResponse(request, "home.html", {
            "actions": recommend.take_action(store, now, ctx.auth_expired),
            "sync": recommend.sync_status(store, now),
            "auto_sync": store.get_setting("auto_sync_plays") == "1",
            "muted_count": len(store.muted_artists()),   # transparency: what's being hidden
            "rediscover": recommend.rediscover_playlists(store, now, epoch=_epoch(store, "rediscover")),
            # Saved albums you haven't played lately — rendered above the playlists in the same
            # section, rotating on the same "rediscover" epoch so the whole block advances together.
            "rediscover_albums": recommend.rediscover_albums(store, now, epoch=_epoch(store, "rediscover")),
            "flash": request.query_params.get("flash"),
            "toast": request.query_params.get("toast"),   # transient success (e.g. re-auth w/ auto-sync)
            # One-time onboarding nudge: once they've synced, point new users at enrichment until
            # they dismiss it (the flag persists across reloads).
            "show_enrich_nudge": bool(store.get_setting("last_sync_at"))
                                 and store.get_setting("enrich_nudge_dismissed") != "1",
            **_feed_context(now),
        })

    @router.post("/onboard/enrich/dismiss")
    def dismiss_enrich_nudge():
        """Permanently dismiss the Home enrichment nudge. Empty 200 so HTMX swaps it out."""
        store.set_setting("enrich_nudge_dismissed", "1")
        return Response(status_code=200)

    @router.get("/privacy")
    def privacy(request: Request):
        return templates.TemplateResponse(request, "privacy.html", {})

    @router.get("/home/generating")
    def home_generating(request: Request):
        """Spinner interstitial shown in the popup we open on the 'Save & play' click, until the save
        round-trip returns and the home tab redirects this window to the new YouTube playlist."""
        return templates.TemplateResponse(request, "generating.html", {})

    @router.get("/home/feed")
    def home_feed(request: Request):
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

    @router.post("/home/refresh-card/{card}")
    def home_refresh_card(request: Request, card: str):
        """Refresh one Home card: advance its rotation to a fresh, unseen slice, rerun that card's
        model, and re-render just that card in place."""
        if card not in _CARD_LABELS:
            return Response(status_code=404)
        now = now_fn()
        RecDao(store).refresh_card(card, max(1, rec_params.get_param(store, "erosion_view_cap")), now)
        p = _one_card(store, card, now)
        if p is None:
            return Response(status_code=204)
        return templates.TemplateResponse(request, "_partials/generated_playlist.html", {"p": p})

    @router.post("/home/breadth")
    async def home_breadth(request: Request):
        # Drag the Breadth bar -> persist the focused<->eclectic bias and return the re-ranked feed in
        # one request (a preview like /home/steer). The bias is a scalar param (not a per-axis weight),
        # so it gets its own route; set_param clamps it to the spec range [-1, 1]. Center (0) is neutral.
        bias = (await request.form()).get("breadth_bias")
        if bias is not None:
            rec_params.set_param(store, "breadth_bias", bias)
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

    @router.post("/home/steer")
    async def home_steer(request: Request):
        # Drag a fingerprint bar -> set a STANDING TRANSIENT LEAN (not a permanent weight) and return
        # the re-ranked feed in one request. The bar shows the EFFECTIVE multiplier (permanent x lean);
        # we store the lean so permanent x lean == the dragged target. Long-term taste is edited only
        # on the Taste Model page. The held lean bakes into permanent over days (graduate_slider_exposure).
        form = await request.form()
        axis, weight = form.get("axis"), form.get("weight")
        if axis and weight and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            target = float(weight)
            perm = store.get_weights().get(axis, 1.0)
            lean = target / perm if perm > 0 else target
            lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX
            lean = max(lo, min(hi, lean))
            store.set_lean(axis, lean, now_fn())
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

    @router.post("/home/fingerprint/expand")
    async def fingerprint_expand(request: Request):
        """Drill into a genre family: return fp-row sliders for each of its subgenres.
        POSTed form field: `family` (e.g. 'techno'). Responds with the subgenre partial."""
        form = await request.form()
        fam = (form.get("family") or "").strip().lower()
        subs = genre_map.subgenres_of(fam)
        w = store.get_weights()
        rows = []
        for sub in subs:
            axis = f"genre:{sub}"
            weight = w.get(axis, 1.0)
            lean = store.get_lean(axis)
            effective = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, weight * lean))
            rows.append({"name": sub, "axis": axis, "effective": effective})
        return templates.TemplateResponse(
            request, "_partials/fingerprint_subgenres.html",
            {"family": fam, "rows": rows})

    @router.post("/home/fingerprint/add")
    async def fingerprint_add(request: Request):
        """Pin an axis (e.g. 'genre:gqom') so it appears as a steerable bar even with zero plays. Writes
        a neutral lean (1.0) so it persists, then returns ONLY the re-rendered genre bars (server-
        authoritative, so dedup/ordering are correct). The client swaps #fp-genre-bars with this and
        leaves the live genre picker untouched — a neutral add doesn't change rankings, so there's no
        need to re-render the whole feed (and re-rendering it would churn the picker)."""
        form = await request.form()
        axis = (form.get("axis") or "").strip()
        if axis and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            # Only write if not already present — don't overwrite a user-set lean.
            if store.get_lean(axis) == 1.0 and axis not in store.get_leans():
                store.set_lean(axis, 1.0, now_fn())
        return templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": recommend.taste_fingerprint(store)})

    @router.get("/home/genres")
    def home_genres():
        """Full genre taxonomy (families + sub-genres) for the Home taste-bar genre picker. Unlike the
        library-scoped clusters list, this is the whole map, so you can pin a bar for a genre you have
        zero plays of yet. Each option: {name, kind: 'family'|'genre'}; the Alpine picker filters it
        client-side (so reset/close are instant) and posts a pick to /home/fingerprint/add."""
        fams = genre_map.all_families()
        seen = set(fams)
        options = [{"name": f, "kind": "family"} for f in fams]
        options += [{"name": g, "kind": "genre"} for g in genre_map.all_genres() if g not in seen]
        return JSONResponse({"options": options})

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
            result["error"] = "Couldn't create it - connect an account and keep at least one track."
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

    @router.get("/home/discover")
    def home_discover(request: Request):
        # From the accumulating discovery pool: recency-biased, repeat-aware, filled in by the
        # background scan of every artist you're interested in (not a top-10 overwrite).
        from yt_playlist.rec import discover
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
        from yt_playlist.rec import discover
        artists = discover.pick_discovered_artists(store, ARTISTS_PER_CARD, now_fn())
        return templates.TemplateResponse(request, "_partials/new_artists.html",
                                          {"artists": artists,
                                           "building": not artists and _busy(),
                                           "stale": bool(artists) and _busy()})

    return router
