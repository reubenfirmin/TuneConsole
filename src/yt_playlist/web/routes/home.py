"""Home tab: the default landing page: Sync control, Take-Action triage, and For-You recs."""
import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from yt_playlist.core import updatecheck
from yt_playlist.library import executor
from yt_playlist.util import genre_map
from yt_playlist.rec import arc_energy, journeys, onboarding, rec_params, recommend, into_recently
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.providers import wikipedia, lastfm
from yt_playlist.web.context import form_float

# How many tracks each generated proto-playlist offers.
PROTO_SIZE = 12
ROTATION_POOL = PROTO_SIZE * 5      # fetch this deep so each epoch's random slice is genuinely fresh
ARTISTS_PER_CARD = 10              # new-artist tiles fetched per epoch (grid caps 5 cols, clamps to 2 rows)
ALBUMS_PER_CARD = 15               # discover album tiles fetched per epoch (grid caps 5 cols, clamps to 3 rows)
# Home cards that rotate. Each holds its content for erosion_view_cap real Home visits, then advances
# to a fresh epoch. They tick together (once per visit, in GET /) but each rotates its OWN pool at its
# own size, so the small new-artist pool cycles through faster than the deep playlist pool.
ROTATING_CARDS = ("wheelhouse", "explore", "comfort", "fresh", "new_artists", "discover",
                  "rediscover", "into_recently", "cards")

# Genre coverage below this fraction of processed tracks, with no Last.fm key, prompts the user to
# add one. Last.fm is the densest genre source, so a thin genre coverage is the signal that a key
# would most help.
LASTFM_NUDGE_COVERAGE = 0.90


LASTFM_NUDGE_SNOOZE_S = 30 * 86400   # dismissing snoozes the nudge 30 days, not forever


def lastfm_nudge_due(store, now=None) -> bool:
    """True when no Last.fm key is set and the nudge isn't snoozed. Dismissing snoozes it for 30 days
    (records a timestamp) so a long-lived install is reminded again instead of never seeing it."""
    if lastfm.available(store):
        return False
    dismissed_at = store.get_setting("lastfm_nudge_dismissed_at")
    if dismissed_at is not None:
        try:
            if (now if now is not None else time.time()) - float(dismissed_at) < LASTFM_NUDGE_SNOOZE_S:
                return False
        except (TypeError, ValueError):
            pass
    return True
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
            "note": _NOTES.get(lane, ""), "tracks": items[:PROTO_SIZE],
            # #50: only the Fresh (out-of-corpus discovery) card carries persistent per-row feedback.
            # Owned-track proto cards stay curate-before-listen (client-side remove only).
            "feedback_surface": "for_you" if lane == "fresh" else None}


def _order_by_journey(store, items, journey, seed):
    """Order proto-card items (ForYouItem objects in preview, plain dicts on the mode path) by the
    rolled DJ JOURNEY, attaching the per-track features journey_order needs (genre, real-audio energy,
    decade, plays, recency). Ordering happens here, not at save, so the preview IS the playlist
    (WYSIWYG): a Save keeps the rows you didn't prune, in the order you see."""
    items = recommend.attach_genres(store, items)
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

    return journeys.journey_order(items, journey, seed, feat)


def _carded(store, lane, label, items, now):
    """A proto-card built from a rolled recipe: roll the theme + journey (seeded by the card's
    rotation epoch so it's stable across steer/stance previews), focus the items on the theme, then
    order them by the rolled JOURNEY (energy arc, eras, deep dive…). Recipes predating journeys fall
    back to 'shuffle'."""
    recipe = recommend.roll_recipe(store, lane, seed=_epoch(store, lane), now=now)
    items = recommend.theme_filter(store, items, recipe.get("facets", {}))
    # roll_recipe forces Fresh to journey="shuffle" (unowned proposals have no plays/recency signal),
    # so the stored recipe and this preview ordering agree. Owned lanes order by their rolled journey.
    items = _order_by_journey(store, items, recipe.get("journey", "shuffle"),
                              recipe.get("dj", {}).get("seed", 0))
    p = _proto(lane, label, items, now)
    p["recipe"] = recipe
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
        from yt_playlist.rec import surfaces
        pool = surfaces.cold_candidates(store, now, limit=PROTO_SIZE)
        items = [surfaces._item_to_fresh_dict(i) for i in pool]
        # NB: #53 offered-count is stamped by the CALLER (the /home/cards loop or the per-card refresh
        # route), not here, so the cold-start fallback path doesn't double-count fresh tracks.
    else:
        return None
    return _carded(store, card, _CARD_LABELS[card], items, now) if items else None


def _epoch(store, card):
    """The card's current rotation epoch: it holds content for erosion_view_cap views, then advances.
    Read-only. The view tick happens in GET / so previews/re-renders don't churn the cards."""
    cap = max(1, rec_params.get_param(store, "erosion_view_cap"))
    return max(0, RecDao(store).card_views(card) - 1) // cap


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _feed_context(now):
        # Bake held slider exposure + sustained listening into the graduation ledger (idempotent per
        # UTC day). The cards themselves now load lazily via /home/cards from the worker's bundles.
        recommend.graduate_slider_exposure(store, now)
        recommend.graduate_play_exposure(store, now)
        return {"fingerprint": recommend.taste_fingerprint(store, now)}

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        dao = RecDao(store)
        for card in ROTATING_CARDS:   # one tick per genuine Home visit -> per-card rotation advances
            dao.bump_card_view(card, now)
        backend_update = updatecheck.update_nudge(store)
        return templates.TemplateResponse(request, "home.html", {
            "actions": recommend.take_action(store, now, ctx.auth_expired),
            "sync": recommend.sync_status(store, now),
            "muted_count": len(store.muted_artists()),   # transparency: what's being hidden
            "rediscover": recommend.rediscover_playlists(store, now, epoch=_epoch(store, "rediscover")),
            # Saved albums you haven't played lately, rendered above the playlists in the same
            # section, rotating on the same "rediscover" epoch so the whole block advances together.
            "rediscover_albums": recommend.rediscover_albums(store, now, epoch=_epoch(store, "rediscover")),
            "flash": request.query_params.get("flash"),
            "toast": request.query_params.get("toast"),   # transient success (e.g. re-auth w/ auto-sync)
            # Sparsity nudge: when metadata is genre-thin and no Last.fm key is set, point the user
            # at adding one (persists once dismissed). The auto-enrich worker handles the rest, so
            # there is no manual-enrichment nag anymore.
            # Welcome nag shown once the first sync lands (dismissable, persists): sets expectations
            # that the model keeps learning over the next week.
            "show_intro": (store.get_setting("last_sync_at") is not None
                           and store.get_setting("intro_dismissed") != "1"),
            "show_lastfm_nudge": lastfm_nudge_due(store, now),
            "backend_update": backend_update,
            "show_backend_update": backend_update is not None,
            # Once the library has synced and the user still has only the default identity, offer the
            # multi-identity merge (dismissable, persists). Skipped as soon as they add a second one.
            "show_identities_nudge": (store.get_setting("last_sync_at") is not None
                                      and len(store.get_identities()) <= 1
                                      and store.get_setting("identities_nudge_dismissed") != "1"),
            "onboarding": onboarding.onboarding_active(store, now),
            "onboard_library": onboarding.library_size(store) >= rec_params.get_param(store, "onboard_library_min"),
            "cleanup_count": onboarding.cleanup_count(store),   # CACHED read, never the O(n^2) scan
            "onboard_progress": onboarding.warmup_progress(store),
            **_feed_context(now),
        })

    @router.post("/onboard/lastfm/dismiss")
    def dismiss_lastfm_nudge():
        """Snooze the Home Last.fm-key nudge for 30 days (records the dismissal time). Empty 200 so
        HTMX swaps it out."""
        store.set_setting("lastfm_nudge_dismissed_at", str(now_fn()))
        return Response(status_code=200)

    @router.post("/onboard/identities/dismiss")
    def dismiss_identities_nudge():
        """Permanently dismiss the multi-identity merge nudge. Empty 200 so HTMX swaps it out."""
        store.set_setting("identities_nudge_dismissed", "1")
        return Response(status_code=200)

    @router.post("/onboard/intro/dismiss")
    def dismiss_intro_nudge():
        """Permanently dismiss the post-first-sync welcome nag. Empty 200 so HTMX swaps it out."""
        store.set_setting("intro_dismissed", "1")
        return Response(status_code=200)

    @router.post("/onboard/update/dismiss")
    def dismiss_update_nudge(v: str = ""):
        """Dismiss the backend-update nag for the version the user saw (passed as ?v=). Falls back to
        the currently-cached latest. A newer release re-shows it. Empty 200 so HTMX swaps it out."""
        dismissed = v or store.get_setting("latest_version_seen")
        if dismissed:
            store.set_setting("backend_update_dismissed_version", dismissed)
        return Response(status_code=200)

    @router.get("/home/onboard/radio")
    def home_onboard_radio(request: Request):
        now = now_fn()
        client = next(iter((ctx.client_provider() or {}).values()), None)
        tracks = onboarding.radio_sample(store, client, now, n=PROTO_SIZE)
        proto = _proto("onboard_radio", "YouTube radio for you", tracks, now) if tracks else None
        return templates.TemplateResponse(request, "_partials/onboard_playlist.html", {"proto": proto})

    @router.get("/home/onboard/library")
    def home_onboard_library(request: Request):
        now = now_fn()
        tracks = onboarding.library_sample(store, n=PROTO_SIZE)
        proto = _proto("onboard_library", "From your library", tracks, now) if tracks else None
        return templates.TemplateResponse(request, "_partials/onboard_playlist.html", {"proto": proto})

    @router.post("/onboard/done")
    def onboard_done():
        store.set_setting("onboard_dismissed", "1")
        return Response(status_code=200, headers={"HX-Refresh": "true"})

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

    @router.get("/home/into-recently")
    def home_into_recently(request: Request):
        """The 'You're into [X] recently' card: a Wikipedia summary for the freshest, least-obvious
        artist/genre in the transient model. Lazy HTMX fragment; empty body when there is nothing
        fresh or no usable page (the card is then simply absent)."""
        now = now_fn()
        # Walk the epoch-ordered pool and render the FIRST subject that resolves to a usable card. The
        # warm (cached) subjects are rotated by epoch so the card erodes through them one per epoch; a
        # pick with no Wikipedia page or no thumbnail falls through to the next instead of blanking the
        # card. Misses are negative-cached, so later refreshes don't refetch the dead subjects.
        for subj in into_recently.subjects_for_epoch(store, now, epoch=_epoch(store, "into_recently")):
            row = store.wiki.get(subj["subject"])
            if row is None or not store.wiki.is_fresh(row, now):
                payload = wikipedia.fetch_summary(subj["kind"], subj["display"])
                store.wiki.put(subj["subject"], subj["kind"], subj["display"], payload, now)
                row = store.wiki.get(subj["subject"])
            if not row or not row["found"] or not row["extract"]:
                continue
            extras = into_recently.decorate(store, subj)
            thumbnail = row["thumbnail"] or extras["thumbnail"]
            if not thumbnail:                          # the card always wants a thumbnail
                continue
            card = {**row, "thumbnail": thumbnail, "color": extras["color"],
                    "seed": extras["seed"], "genre": extras["genre"], "depth": into_recently.CLUSTER_DEPTH}
            return templates.TemplateResponse(request, "_partials/into_recently.html", {"card": card})
        return Response(status_code=200)

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
        keys = [(t.get("key") if isinstance(t, dict) else getattr(t, "key", None)) for t in p["tracks"]]
        store.mark_offered("track", [k for k in keys if k], now)   # #53: this route is the sole counter here
        return templates.TemplateResponse(request, "_partials/generated_playlist.html", {"p": p})

    def _cards_fragment(request, now):
        from yt_playlist.rec import mode_surfaces
        epoch = _epoch(store, "cards")
        cards = mode_surfaces.assemble_cards(store, now, epoch)
        if cards:
            protos = []
            for c in cards:
                # Roll a DJ journey for this card so the saved playlist carries a Flow (parity with the
                # per-lane _carded cards). Seed per (epoch, mode) so each of the 4 cards rolls its OWN
                # journey, stable across re-renders of the same epoch. We keep only the rolled journey
                # + dj seed: the mode already defines the theme, so its facets/weights are not re-rolled.
                rolled = recommend.roll_recipe(store, c["lane"], seed=f"{epoch}:{c['mode_id']}", now=now)
                journey, dj = rolled.get("journey", "shuffle"), rolled.get("dj", {})
                tracks = _order_by_journey(store, c["tracks"], journey, dj.get("seed", 0))
                protos.append(_proto(c["lane"], c["label"], tracks, now)
                              | {"mode_id": c["mode_id"],
                                 "recipe": {"model": "mode", "mode_id": c["mode_id"],
                                            "journey": journey, "dj": dj}})
            try:
                store.modes.log_impressions(epoch, [(c["lane"], c["mode_id"]) for c in cards], now)
            except Exception:  # noqa: BLE001 - logging must never break the card render
                ctx.logger.warning("mode-impression log failed", exc_info=True)
        else:
            # Fallback before the first rebuild: the pre-B per-card builders, so the row is never empty.
            protos = [p for p in (_one_card(store, name, now) for name in
                                  ("wheelhouse", "explore", "comfort", "fresh")) if p]
        for p in protos:                                   # #53 offered-count parity
            # tracks are dicts on the mode path but ForYouItem objects on the _one_card fallback,
            # so read the key from either shape (a bare t.get here 500s the whole card row).
            keys = [(t.get("key") if isinstance(t, dict) else getattr(t, "key", None))
                    for t in p["tracks"]]
            store.mark_offered("track", [k for k in keys if k], now)
        return templates.TemplateResponse(request, "_partials/mode_cards.html", {"protos": protos})

    def _cards_safe(request, now):
        # The row loads via hx-trigger=load with the spinner as placeholder content: a 500 here leaves
        # the spinner up forever (HTMX does not swap on error). So never raise out of this surface,
        # render an empty row on any unexpected failure (logged) so the spinner always clears.
        try:
            return _cards_fragment(request, now)
        except Exception:  # noqa: BLE001 - a render failure must not strand the home page on a spinner
            ctx.logger.exception("home cards render failed")
            return templates.TemplateResponse(request, "_partials/mode_cards.html", {"protos": []})

    @router.get("/home/cards")
    def home_cards(request: Request):
        """The mode-driven card row: 4 distinct taste-mode variations from the worker's prepared
        bundles. Lazy fragment (spinner) so live mood tilt runs off the main render."""
        return _cards_safe(request, now_fn())

    @router.post("/home/refresh-cards")
    def home_refresh_cards(request: Request):
        """Re-roll the whole mode-card row (advance its shared rotation epoch)."""
        now = now_fn()
        RecDao(store).refresh_card("cards", max(1, rec_params.get_param(store, "erosion_view_cap")), now)
        return _cards_safe(request, now)

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
        target = form_float(weight)
        if axis and target is not None and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            perm = store.get_weights().get(axis, 1.0)
            lean = target / perm if perm > 0 else target
            lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX
            lean = max(lo, min(hi, lean))
            store.set_lean(axis, lean, now_fn())
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

    @router.post("/home/fingerprint/add")
    async def fingerprint_add(request: Request):
        """Pin an axis (e.g. 'genre:gqom') so it appears as a steerable bar even with zero plays. Writes
        a neutral lean (1.0) so it persists, then returns ONLY the re-rendered genre bars (server-
        authoritative, so dedup/ordering are correct). The client swaps #fp-genre-bars with this and
        leaves the live genre picker untouched. A neutral add doesn't change rankings, so there's no
        need to re-render the whole feed (and re-rendering it would churn the picker)."""
        form = await request.form()
        axis = (form.get("axis") or "").strip()
        if axis and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            store.unhide_facet(axis)                  # re-adding an axis un-hides it (it was removed before)
            # Only write if not already present. Don't overwrite a user-set lean.
            if store.get_lean(axis) == 1.0 and axis not in store.get_leans():
                store.set_lean(axis, 1.0, now_fn())
        return templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": recommend.taste_fingerprint(store, now_fn())})

    @router.post("/home/fingerprint/remove")
    async def fingerprint_remove(request: Request):
        """Remove one bar from the Home panel: hide the axis (display-only) AND clear any standing lean
        so it stops steering. Works for ANY bar: a top played family, a steered family, or an added
        niche, all simply disappear. Returns the re-rendered bars (#fp-genre-bars swap, picker left
        alive). Permanent taste (the Taste page) is untouched; the genre picker re-adds it any time."""
        axis = ((await request.form()).get("axis") or "").strip()
        if axis:
            store.hide_facet(axis)
            store.clear_lean(axis)
        return templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": recommend.taste_fingerprint(store, now_fn())})

    @router.post("/home/fingerprint/reset")
    async def fingerprint_reset(request: Request):
        """Reset Home steering to default: wipe every standing lean, un-hide every removed bar, and
        re-center the breadth bias. Re-ranks the feed (so swaps the whole #home-feed). Permanent
        weights (long-term taste, on the Taste page) are deliberately left alone."""
        store.clear_all_leans()
        store.clear_hidden_facets()
        rec_params.set_param(store, "breadth_bias", rec_params.PARAMS_BY_NAME["breadth_bias"].default)
        return templates.TemplateResponse(request, "_partials/home_feed.html", _feed_context(now_fn()))

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
                # Land playback in the already-open YouTube Music tab, not a new one. The watch URL
                # (list=...) autoplays the playlist. The extension swaps the existing tab; the result
                # template never opens a YouTube tab itself.
                watch_url = f"https://music.youtube.com/watch?list={res['new_ytm']}"
                bridge = getattr(ctx, "bridge", None)
                if bridge is not None and getattr(bridge, "connected", False):
                    try:
                        bridge.send_control({"type": "navigate", "url": watch_url})
                    except Exception:  # noqa: BLE001 - navigation is best-effort
                        pass
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
                if isinstance(recipe, dict) and recipe.get("mode_id") is not None and res.get("pid"):
                    try:
                        store.modes.log_pick(res["pid"], int(recipe["mode_id"]), now_fn())
                    except Exception:  # noqa: BLE001 - logging must never break playlist creation
                        pass
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
