"""Home tab: the default landing page: Sync control, Take-Action triage, and For-You recs."""
import asyncio
from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for,main_client
import json
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from yt_playlist.core import updatecheck
from yt_playlist.library import executor
from yt_playlist.util import genre_map
from yt_playlist.rec import actions, graduation, home_cards, home_themes, home_vibe, mix_tuning, onboarding, rec_params, surfaces, taste_analysis, into_recently
from yt_playlist.rec import measurement
from yt_playlist.rec.home_cards import _epoch, _proto
from yt_playlist.rec.trend_rollups import last_complete_month
from yt_playlist.providers import wikipedia, lastfm
from yt_playlist.web.context import form_float
from yt_playlist.web.jobs import start_fresh_pool, start_library_pool, start_playlist_enrichment
from yt_playlist.web.spotify_import import current_job as current_spotify_import_job
from yt_playlist.web.theme import genre_tint

# How many tracks each generated proto-playlist offers.
PROTO_SIZE = home_cards.PROTO_SIZE
ARTISTS_PER_CARD = 10              # new-artist tiles fetched per epoch (grid caps 5 cols, clamps to 2 rows)
ALBUMS_PER_CARD = 15               # discover album tiles fetched per epoch (grid caps 5 cols, clamps to 3 rows)
# Home cards that rotate. Each holds its content for erosion_view_cap real Home visits, then advances
# to a fresh epoch. They tick together (once per visit, in GET /) but each rotates its OWN pool at its
# own size, so the small new-artist pool cycles through faster than the deep playlist pool.
ROTATING_CARDS = ("wheelhouse", "explore", "comfort", "fresh", "new_artists", "discover",
                  "rediscover", "into_recently", "cards")
MENU_MAX_AGE_S = 86400  # a lightly visited Home should not hold the same themes for days

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
    dismissed_at = store.settings.get_setting("lastfm_nudge_dismissed_at")
    if dismissed_at is not None:
        try:
            if (now if now is not None else time.time()) - float(dismissed_at) < LASTFM_NUDGE_SNOOZE_S:
                return False
        except (TypeError, ValueError):
            pass
    return True


TAKEOUT_NAG_SNOOZE_S = 90 * 86400   # dismissing snoozes the nag 90 days, not forever


def takeout_nag_due(store, now=None) -> bool:
    """True when a Google Takeout watch-history import has never landed and the nag isn't snoozed.
    Terminal (never due again) once takeout_imported_at is set. No last_sync gate: importing early
    is exactly the point, so this can be due on a fresh install. Dismissing snoozes it for 90 days
    (records a timestamp) so a long-lived install that never imports is reminded again."""
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


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    class PoolJobsAdapter:
        def latest(self, kind, parameters):
            return ctx.jobs.latest(kind, parameters)

        def start(self, kind, parameters, excluded):
            return (start_fresh_pool(ctx, parameters) if kind == "fresh_pool"
                    else start_library_pool(ctx, parameters, excluded))

    recommendations = home_cards.HomeCards(store, PoolJobsAdapter(), ctx.logger)

    def _tuning_active():
        genres = store.rec_surface.get_proposals("home_genre_request") or {}
        return (bool(store.rec_model.get_leans()) or bool(store.rec_model.hidden_facets())
                or bool(store.settings.get_setting(home_vibe.SETTING))
                or rec_params.get_param(store, "breadth_bias") != 0
                or (genres.get("epoch") == _epoch(store, "cards")
                    and bool(genres.get("include_genres") or genres.get("avoid_genres"))))

    def _tuning_response(response, now):
        response.headers["X-Mix-Tuning-Remaining"] = str(mix_tuning.remaining(store, now))
        response.headers["X-Mix-Tuning-Active"] = str(_tuning_active()).lower()
        return response

    def _audit_cards(request, protos, now):
        recommendations.audit(protos, now, request.url.path)

    def _feed_context(now):
        # Bake held slider exposure + sustained listening into the graduation ledger (idempotent per
        # UTC day). The cards themselves now load lazily via /home/cards from the worker's bundles.
        graduation.graduate_slider_exposure(store, now)
        graduation.graduate_play_exposure(store, now)
        return {"fingerprint": taste_analysis.taste_fingerprint(store, now),
                "tuning_active": _tuning_active(),
                "vibe_presets": home_vibe.PRESETS, "vibe": home_vibe.current(store, now),
                "has_modes": bool(store.modes.list_modes(active_only=True))}

    @router.get("/")
    def home_page(request: Request):
        now = now_fn()
        dao = store.rec_surface
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
        _story = (store.rec_surface.get_proposals("trend_rollups") or {}).get("story")
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
        radio_tracks = (store.charts.playlist_tracks_detail(radio_state["local_id"])
                        if radio_state.get("active") and radio_state.get("local_id") else [])
        return templates.TemplateResponse(request, "home.html", {
            "radio_bootstrap": radio_bootstrap,
            "radio_tracks": radio_tracks,
            "actions": actions.take_action(store, now, ctx.auth_expired),
            "sync": actions.sync_status(store, now),
            "muted_count": len(store.rec_model.muted_artists()),   # transparency: what's being hidden
            "rediscover": surfaces.rediscover_playlists(store, now, epoch=_epoch(store, "rediscover")),
            # Saved albums you haven't played lately, rendered above the playlists in the same
            # section, rotating on the same "rediscover" epoch so the whole block advances together.
            "rediscover_albums": surfaces.rediscover_albums(store, now, epoch=_epoch(store, "rediscover")),
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
            # Takeout import nag: no last_sync gate (importing early is exactly the point), terminal
            # once takeout_imported_at is set by a successful import.
            "show_takeout_nag": takeout_nag_due(store, now),
            "backend_update": backend_update,
            "show_backend_update": backend_update is not None,
            # Once the library has synced and the user still has only the default identity, offer the
            # multi-identity merge (dismissable, persists). Skipped as soon as they add a second one.
            "show_identities_nudge": (store.settings.get_setting("last_sync_at") is not None
                                      and len(store.identities.get_identities()) <= 1
                                      and store.settings.get_setting("identities_nudge_dismissed") != "1"),
            "onboarding": onboarding.onboarding_active(store, now),
            "onboard_library": onboarding.library_size(store) >= rec_params.get_param(store, "onboard_library_min"),
            "cleanup_count": onboarding.cleanup_count(store),   # CACHED read, never the O(n^2) scan
            "onboard_progress": onboarding.warmup_progress(store),
            "recap": recap,
            # Persisted background-import state. Completion stays in Needs attention with its full
            # report until explicitly dismissed, including across app/browser restarts.
            "spotify_library_job": spotify_library_job,
            **_feed_context(now),
        })

    @router.post("/onboard/lastfm/dismiss")
    def dismiss_lastfm_nudge():
        """Snooze the Home Last.fm-key nudge for 30 days (records the dismissal time). Empty 200 so
        HTMX swaps it out."""
        store.settings.set_setting("lastfm_nudge_dismissed_at", str(now_fn()))
        return Response(status_code=200)

    @router.post("/onboard/takeout/dismiss")
    def dismiss_takeout_nag():
        """Snooze the Home Takeout-import nag for 90 days (records the dismissal time). Empty 200 so
        HTMX swaps it out."""
        store.settings.set_setting("takeout_nag_dismissed_at", str(now_fn()))
        return Response(status_code=200)

    @router.post("/onboard/identities/dismiss")
    def dismiss_identities_nudge():
        """Permanently dismiss the multi-identity merge nudge. Empty 200 so HTMX swaps it out."""
        store.settings.set_setting("identities_nudge_dismissed", "1")
        return Response(status_code=200)

    @router.post("/onboard/intro/dismiss")
    def dismiss_intro_nudge():
        """Permanently dismiss the post-first-sync welcome nag. Empty 200 so HTMX swaps it out."""
        store.settings.set_setting("intro_dismissed", "1")
        return Response(status_code=200)

    @router.post("/onboard/recap/dismiss")
    def dismiss_recap_nudge(month: str = ""):
        """Dismiss the monthly-recap nag for the given month; next month's recap re-shows. Empty 200 so
        HTMX swaps it out."""
        if month:
            store.settings.set_setting("recap_dismissed_month", month)
        return Response(status_code=200)

    @router.post("/onboard/update/dismiss")
    def dismiss_update_nudge(v: str = ""):
        """Dismiss the backend-update nag for the version the user saw (passed as ?v=). Falls back to
        the currently-cached latest. A newer release re-shows it. Empty 200 so HTMX swaps it out."""
        dismissed = v or store.settings.get_setting("latest_version_seen")
        if dismissed:
            store.settings.set_setting("backend_update_dismissed_version", dismissed)
        return Response(status_code=200)

    @router.get("/home/onboard/radio")
    def home_onboard_radio(request: Request):
        now = now_fn()
        client = main_client(store,ctx.client_provider())
        tracks = onboarding.radio_sample(store, client, now, n=PROTO_SIZE)
        proto = _proto(store, "onboard_radio", "YouTube radio for you", tracks, now) if tracks else None
        return templates.TemplateResponse(request, "_partials/onboard_playlist.html", {"proto": proto})

    @router.get("/home/onboard/library")
    def home_onboard_library(request: Request):
        now = now_fn()
        tracks = onboarding.library_sample(store, n=PROTO_SIZE)
        proto = _proto(store, "onboard_library", "From your library", tracks, now) if tracks else None
        return templates.TemplateResponse(request, "_partials/onboard_playlist.html", {"proto": proto})

    @router.post("/onboard/done")
    def onboard_done():
        store.settings.set_setting("onboard_dismissed", "1")
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
        # Walk the epoch-ordered pool and render up to three usable subjects. A single card was too
        # slight to justify a whole dashboard area; a small editorial stack makes the transient model
        # legible without stretching one Wikipedia extract beyond its value. Misses are negative-cached.
        cards = []
        for subj in into_recently.subjects_for_epoch(store, now, epoch=_epoch(store, "into_recently")):
            row = store.wiki.get(subj["subject"])
            if store.wiki.should_refresh(subj["subject"], row, now):
                into_recently.refresh_summary(store, subj, now, wikipedia.fetch_summary)
                row = store.wiki.get(subj["subject"])
            if not row or not row["found"] or not row["extract"]:
                continue
            extras = into_recently.decorate(store, subj)
            thumbnail = row["thumbnail"] or extras["thumbnail"]
            if not thumbnail:                          # the card always wants a thumbnail
                continue
            card = {**row, "thumbnail": thumbnail, "color": extras["color"],
                    "seed": extras["seed"], "genre": extras["genre"], "depth": into_recently.CLUSTER_DEPTH}
            cards.append(card)
            if len(cards) == 3:
                break
        if cards:
            return templates.TemplateResponse(request, "_partials/into_recently.html",
                                              {"cards": cards, "card": cards[0]})
        return Response(status_code=200)

    @router.post("/home/refresh-card/{card}")
    def home_refresh_card(request: Request, card: str):
        """Refresh one Home card: advance its rotation to a fresh, unseen slice, rerun that card's
        model, and re-render just that card in place."""
        if card not in home_cards._CARD_LABELS:
            return Response(status_code=404)
        now = now_fn()
        p = recommendations.refresh_card(card, now)
        if p is None:
            return Response(status_code=204)
        response = templates.TemplateResponse(request, "_partials/generated_playlist.html",
                                               {"p": p, "regenerating": True})
        _audit_cards(request, [p], now)
        return response

    def _cards_fragment(request, now, *, regenerating=False, use_genre_request=True, genre_notice=None,
                        swap_oob=False):
        result = recommendations.cards(home_cards.CardsRequest(
            now, regenerating, use_genre_request, genre_notice))
        # Presentation color remains with the HTTP/template adapter. Avoid asdict here:
        # protos may contain ForYouItem rows, which must retain their existing shape.
        context = vars(result).copy()
        context["genre_options"] = [option | {"tint": genre_tint(option["family"])}
                                    for option in result.genre_options]
        response = templates.TemplateResponse(request, "_partials/mode_cards.html",
                                               {**context, "regenerating": regenerating, "swap_oob": swap_oob})
        _audit_cards(request, result.protos, now)
        return _tuning_response(response, now)

    def _pool_fragment(request, job):
        result = recommendations.pool(job, now_fn(), request.url.path)
        if result.proto:
            return templates.TemplateResponse(request, "_partials/generated_playlist.html", {"p": result.proto})
        return templates.TemplateResponse(request, "_partials/pool_card.html",
                                          {"pool": result.pool})

    @router.post("/home/fresh-pool")
    async def home_fresh_pool(request: Request):
        form = await request.form()
        parameters = {key: sorted(set(form.getlist(key))) for key in ("include_genres", "avoid_genres")}
        available = {o["family"] for o in home_themes.genre_options(store)}
        if any(len(form.getlist(key)) > 100 or any(g not in available for g in values)
               for key, values in parameters.items()):
            return Response(status_code=400)
        return await asyncio.to_thread(_pool_fragment, request, recommendations.start_pool("fresh_pool", parameters, []))

    @router.get("/home/fresh-pool/{job_id}")
    def home_fresh_pool_status(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None or job.kind != "fresh_pool":
            return _expired_pool_fragment(request)
        return _pool_fragment(request, job)

    def _expired_pool_fragment(request):
        # HTMX leaves error responses unswapped, so polling needs a successful terminal
        # fragment. The registry no longer has the original parameters; offer a new menu
        # rather than retaining expired jobs for retries.
        return templates.TemplateResponse(
            request, "_partials/pool_expired.html", {},
            status_code=200 if request.headers.get("HX-Request") == "true" else 404)

    @router.post("/home/library-pool")
    async def home_library_pool(request: Request):
        form = await request.form()
        try:
            parameters = {key: sorted(set(form.getlist(key))) for key in ("include_genres", "avoid_genres")}
            parameters.update(lane=form.get("lane"), epoch=int(form.get("epoch", "-1")))
            excluded = json.loads(form.get("exclude_keys") or "[]")
        except (TypeError, ValueError):
            return Response(status_code=400)
        available = {o["family"] for o in home_themes.genre_options(store)}
        if (parameters["lane"] not in ("wheelhouse", "explore", "comfort", "temporal")
                or parameters["epoch"] != _epoch(store, "cards")
                or any(len(form.getlist(key)) > 100 or any(g not in available for g in parameters[key])
                       for key in ("include_genres", "avoid_genres"))
                or not isinstance(excluded, list) or len(excluded) > 200
                or any(not isinstance(k, str) or len(k) > 1000 for k in excluded)):
            return Response(status_code=400)
        return await asyncio.to_thread(_pool_fragment, request, recommendations.start_pool("library_pool", parameters, excluded))

    @router.get("/home/library-pool/{job_id}")
    def home_library_pool_status(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None or job.kind != "library_pool":
            return _expired_pool_fragment(request)
        return _pool_fragment(request, job)

    def _cards_safe(request, now, *, swap_oob=False):
        # The row loads via hx-trigger=load with the spinner as placeholder content: a 500 here leaves
        # the spinner up forever (HTMX does not swap on error). So never raise out of this surface,
        # render an empty row on any unexpected failure (logged) so the spinner always clears.
        try:
            return _cards_fragment(request, now, swap_oob=swap_oob)
        except Exception:  # noqa: BLE001 - a render failure must not strand the home page on a spinner
            ctx.logger.exception("home cards render failed")
            return templates.TemplateResponse(request, "_partials/mode_cards.html",
                                               {"protos": [], "swap_oob": swap_oob})

    @router.get("/home/cards")
    def home_cards_fragment(request: Request):
        """The mode-driven card row: 4 distinct taste-mode variations from the worker's prepared
        bundles. Lazy fragment (spinner) so live mood tilt runs off the main render."""
        return _cards_safe(request, now_fn())

    @router.post("/home/refresh-cards")
    def home_refresh_cards(request: Request):
        """Re-roll the whole mode-card row (advance its shared rotation epoch)."""
        now = now_fn()
        store.rec_surface.refresh_card("cards", max(1, rec_params.get_param(store, "erosion_view_cap")), now)
        try:
            return _cards_fragment(request, now, regenerating=True)
        except Exception:  # noqa: BLE001 - preserve the same safe-fragment contract as the GET
            ctx.logger.exception("home cards refresh render failed")
            return templates.TemplateResponse(request, "_partials/mode_cards.html", {"protos": []})

    @router.post("/home/other-genres")
    async def home_other_genres(request: Request):
        """One-menu request based on the genres in this browser's currently displayed cards.
        Expires with the next rotation; no lean, permanent weight or dislike is written."""
        try:
            genres = json.loads((await request.form()).get("genres") or "[]")
        except (TypeError, ValueError):
            return Response(status_code=400)
        if not isinstance(genres, list) or not genres or len(genres) > 100 or any(
                not isinstance(g, str) or not g.strip() or len(g) > 100 for g in genres):
            return Response(status_code=400)
        now = now_fn()
        with mix_tuning.adjusting(store, now):
            store.rec_surface.refresh_card("cards", max(1, rec_params.get_param(store, "erosion_view_cap")), now)
            store.rec_surface.put_proposals("home_genre_request", {
                "epoch": _epoch(store, "cards"),
                "avoid_genres": sorted({genre_map.family(g) for g in genres}),
            }, now)
        return await asyncio.to_thread(_cards_safe, request, now)

    @router.post("/home/mix-genres")
    async def home_mix_genres(request: Request):
        """Choose genre families for one menu, without teaching a lasting preference."""
        genres = (await request.form()).getlist("genres")
        available = {option["family"] for option in home_themes.genre_options(store)}
        if not genres or len(genres) > 100 or any(not isinstance(g, str) or g not in available for g in genres):
            return Response(status_code=400)
        now = now_fn()
        with mix_tuning.adjusting(store, now):
            store.rec_surface.refresh_card("cards", max(1, rec_params.get_param(store, "erosion_view_cap")), now)
            store.rec_surface.put_proposals("home_genre_request", {
                "epoch": _epoch(store, "cards"), "include_genres": sorted(set(genres)),
            }, now)
        return await asyncio.to_thread(_cards_safe, request, now)

    @router.post("/home/vibe")
    async def home_vibe_change(request: Request):
        vibe = (await request.form()).get("vibe", "")
        if vibe and vibe not in home_vibe.PRESETS:
            return Response(status_code=400)
        now = now_fn()
        with mix_tuning.adjusting(store, now):
            if vibe:
                store.settings.set_setting(home_vibe.SETTING, vibe)
            else:
                store.settings.delete_setting(home_vibe.SETTING)
        return await asyncio.to_thread(_cards_safe, request, now)

    @router.post("/home/breadth")
    async def home_breadth(request: Request):
        # Drag the Breadth bar -> persist the focused<->eclectic bias and return only the re-ranked
        # four-card board. The bias is a scalar param (not a per-axis weight),
        # so it gets its own route; set_param clamps it to the spec range [-1, 1]. Center (0) is neutral.
        bias = (await request.form()).get("breadth_bias")
        now = now_fn()
        if bias is not None:
            with mix_tuning.adjusting(store, now):
                rec_params.set_param(store, "breadth_bias", bias)
        return await asyncio.to_thread(_cards_safe, request, now)

    @router.post("/home/steer")
    async def home_steer(request: Request):
        # Drag a fingerprint bar -> set a temporary lean and return
        # only the re-ranked four-card board. The bar shows the EFFECTIVE multiplier (permanent x lean);
        # we store the lean so permanent x lean == the dragged target. Long-term taste is edited only
        # on the Taste Model page. These adjustments expire after four hours.
        form = await request.form()
        axis, weight = form.get("axis"), form.get("weight")
        target = form_float(weight)
        if axis and target is not None and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            now = now_fn()
            with mix_tuning.adjusting(store, now):
                perm = store.rec_model.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d")).get(axis, 1.0)
                lean = target / perm if perm > 0 else target
                lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX
                lean = max(lo, min(hi, lean))
                store.rec_model.set_lean(axis, lean, now)
        return await asyncio.to_thread(_cards_safe, request, now_fn())

    @router.post("/home/fingerprint/add")
    async def fingerprint_add(request: Request):
        """Pin an axis (e.g. 'genre:gqom') so it appears as a steerable bar even with zero plays. Writes
        a neutral lean (1.0) so it persists, then returns ONLY the re-rendered genre bars (server-
        authoritative, so dedup/ordering are correct). The client swaps #fp-genre-bars with this and
        leaves the live genre picker untouched. A neutral add doesn't change rankings, so there's no
        need to re-render the whole feed (and re-rendering it would churn the picker)."""
        form = await request.form()
        axis = (form.get("axis") or "").strip()
        now = now_fn()
        if axis and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            with mix_tuning.adjusting(store, now):
                store.rec_model.unhide_facet(axis)              # re-adding an axis un-hides it
                # Only write if not already present. Don't overwrite a user-set lean.
                if store.rec_model.get_lean(axis) == 1.0 and axis not in store.rec_model.get_leans():
                    store.rec_model.set_lean(axis, 1.0, now)
        return _tuning_response(templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": taste_analysis.taste_fingerprint(store, now)}), now)

    @router.post("/home/fingerprint/remove")
    async def fingerprint_remove(request: Request):
        """Remove one bar from the Home panel: hide the axis (display-only) AND clear any standing lean
        so it stops steering. Works for ANY bar: a top played family, a steered family, or an added
        niche, all simply disappear. Returns the re-rendered bars (#fp-genre-bars swap, picker left
        alive). Permanent taste (the Taste page) is untouched; the genre picker re-adds it any time."""
        axis = ((await request.form()).get("axis") or "").strip()
        now = now_fn()
        if axis:
            with mix_tuning.adjusting(store, now):
                store.rec_model.hide_facet(axis)
                store.rec_model.clear_lean(axis)
        return _tuning_response(templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": taste_analysis.taste_fingerprint(store, now)}), now)

    def _tuning_fragment(request, now):
        controls = templates.TemplateResponse(
            request, "_partials/taste_fingerprint.html",
            {"fingerprint": taste_analysis.taste_fingerprint(store, now), "tuning_active": _tuning_active(),
             "vibe_presets": home_vibe.PRESETS, "vibe": home_vibe.current(store, now)})
        cards = _cards_safe(request, now, swap_oob=True)
        return _tuning_response(Response(controls.body + cards.body, media_type="text/html"), now)

    @router.get("/home/fingerprint")
    def fingerprint_status(request: Request):
        # Recheck the persisted deadline: another tab may have extended the tuning window.
        now = now_fn()
        mix_tuning.expire(store, now)
        return _tuning_fragment(request, now)

    @router.post("/home/fingerprint/reset")
    def fingerprint_reset(request: Request):
        """Clear all manual Home choices and refresh both controls and mixes together."""
        mix_tuning.reset(store)
        response = _tuning_fragment(request, now_fn())
        # Close before replacing the controls so their remembered open state cannot reopen them.
        response.headers["HX-Trigger"] = "home-mixes-applied"
        return response

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
        plays = {g.lower(): count for g, count in store.rec_query.genre_play_distribution().items()}
        family_plays = {}
        for genre, count in plays.items():
            family = genre_map.family(genre)
            family_plays[family] = family_plays.get(family, 0) + count
        options.sort(key=lambda o: (-(family_plays.get(o["name"], 0) if o["kind"] == "family"
                                     else plays.get(o["name"], 0)), o["name"]))
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
        account_error = None
        try:
            identity_id,client = client_for(store,ctx.client_provider())
        except ValueError as exc:
            identity_id,client,account_error = None,None,str(exc)
        result = {"name": name}
        if client is None or not tracks:
            result["error"] = account_error or "Keep at least one track to create a playlist."
        else:
            try:
                res = await activity_for(ctx).to_thread(
                    executor.create_generated_playlist, store, name, tracks, client, now_fn(),
                    identity_id, recipe=recipe)
                if (res.get("pid") and isinstance(recipe, dict)
                        and (recipe.get("model") == "fresh" or recipe.get("lane") == "fresh")):
                    start_playlist_enrichment(ctx, res["pid"])
                # Land playback in the already-open YouTube Music tab, not a new one. The watch URL
                # (list=...) autoplays the playlist. But YouTube returns the new id before the
                # watch?list= page is loadable, so the switch waits until get_playlist confirms the
                # playlist exists (executor.navigate_when_ready), on a background thread so this
                # response is not held for the poll. The result template never opens a YouTube tab.
                watch_url = f"https://music.youtube.com/watch?list={res['new_ytm']}"
                bridge = getattr(ctx, "bridge", None)
                if bridge is not None and getattr(bridge, "connected", False):
                    activity_for(ctx).start_thread(executor.navigate_when_ready,
                        args=(client, bridge, res["new_ytm"], watch_url))
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
                if res.get("pid") and res.get("added"):
                    measurement.record_action(store, "save", now_fn(), playlist_id=res["pid"],
                                              event_id=f"generate:{res['new_ytm']}", generated=True)
                if isinstance(recipe, dict) and recipe.get("mode_id") is not None and res.get("pid"):
                    try:
                        store.modes.log_pick(res["pid"], int(recipe["mode_id"]), now_fn(),
                                             ranker=recipe.get("ranker"))
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
