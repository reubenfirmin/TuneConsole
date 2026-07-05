"""WebSocket endpoint the browser extension connects to.

Authentication is by ORIGIN, not a shared token: the browser stamps every extension-initiated
WebSocket handshake with `Origin: chrome-extension://<id>`, and a web page cannot forge that header.
So we accept the socket only when it comes from our pinned extension id, which makes pairing seamless
(install the extension and it connects, nothing to paste) while still rejecting any local web page
that tries to drive the bridge. No credential ever crosses this socket in either direction."""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from yt_playlist.library import executor, live_plays, player_events
from yt_playlist.rec import radio as radio_mod, rec_params, recommend
from yt_playlist.web.context import form_float

logger = logging.getLogger(__name__)

# The published (Chrome Web Store) build and a local unpacked load of extension/ have DIFFERENT ids:
# the store signs with its own key, while an unpacked load derives its id from the manifest `key`
# field. Both are first-party builds of the same extension, so both origins may open the bridge.
EXTENSION_ID = "opmmbolbdgdkphfocffpjpgmelkkjkcn"        # Chrome Web Store build
DEV_EXTENSION_ID = "edhplcadobipneepllhkkajckoammpnk"    # unpacked extension/ (from manifest `key`)
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
EXTENSION_ORIGINS = {EXTENSION_ORIGIN, f"chrome-extension://{DEV_EXTENSION_ID}"}


def build(ctx) -> APIRouter:
    router = APIRouter()
    bridge = ctx.bridge
    templates = getattr(ctx, "templates", None)

    @router.get("/bridge/status")
    def bridge_status():
        radio = getattr(ctx, "radio", None)
        # Mode transparency + fallback diagnostics (visibility wave): computed under radio.lock so a
        # concurrent WS deck-ready/on_play mutation is never read half-applied. Keys are always
        # present (empty/None/False when radio is absent or inactive) so the UI never has to guard.
        radio_dual, radio_fallback_reason, radio_upcoming = False, None, []
        if radio is not None:
            with radio.lock:
                radio_dual = bool(radio.dual_deck)
                radio_fallback_reason = getattr(radio, "fallback_reason", None)
                radio_upcoming = radio_mod.upcoming_picks(radio)
        return {"connected": bridge.connected, "now_playing": bridge.now_playing,
                "radio": bool(radio is not None and radio.active),
                "radio_waiting": bool(radio is not None and getattr(radio, "waiting", False)),
                "radio_dual": radio_dual,
                "radio_fallback_reason": radio_fallback_reason,
                "radio_upcoming": radio_upcoming}

    def _radio_client():
        # (identity_id, client) for the master account, or (None, None) when not connected.
        return next(iter((ctx.client_provider() or {}).items()), (None, None))

    # Radio applies run here, NEVER awaited from the WS receive loop: the reconcile inside issues
    # bridge requests over that same socket, and their responses can only be read by that loop, so
    # awaiting deadlocks until the 30s bridge timeout (seen live 2026-07-04: every mid-session
    # append silently died). One worker = applies stay serialized (FIFO), no interleaved commits.
    _radio_apply_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radio-apply")

    def _radio_apply(session, plan):
        # Best-effort: reconcile the playlist to the desired vids (only when it changed), and commit
        # session.applied_vids ONLY once that reconcile call returns without raising. A failed
        # reconcile (network hiccup, revoked auth, etc.) leaves applied_vids stale on purpose: the
        # next identical desired_vids from a later on_play still differs from the stale applied_vids,
        # so it is retried instead of being silently dropped as "already applied". Re-primes the
        # boundary re-sync last. Never raises into the WS loop.
        try:
            vids = plan.get("desired_vids")
            ytm = getattr(session, "playlist_ytm", None)
            if vids is not None and ytm:
                _ident, client = _radio_client()
                if client is not None:
                    executor._reconcile(client, ytm, vids)
                    with session.lock:
                        session.applied_vids = vids
            pr = plan.get("prime")
            if pr and ytm:
                bridge.send_control({"type": "radio-prime",
                                     "url": radio_mod.playlist_watch_url(pr["video_id"], ytm),
                                     "videoId": pr["video_id"]})
        except Exception:  # noqa: BLE001 - control sends must never raise into a caller
            logger.warning("radio apply failed", exc_info=True)

    def _deck_reconcile_navigate(session, plan):
        # Reconcile one deck's mini playlist by ytm id, then point/recreate its tab and arm its boundary.
        #
        # Never bake role or playlist_ytm at plan time (DEFECT 1, T7g review): `plan` is built by
        # rebuild_standby well before this runs (it is handed to the apply pool), and
        # executor._reconcile's own network round trip below gives a concurrent, confirmed
        # deck-toggled an even wider window to land in between. So the current epoch and the deck's
        # CURRENT role/playlist_ytm are re-resolved from session state under session.lock TWICE, each
        # time immediately before the network call that follows:
        #   1. immediately before the reconcile: drop silently (stale plan; a toggle already
        #      intervened before we even started) if plan_epoch != session.epoch, else resolve ytm
        #      fresh.
        #   2. immediately before the sends: re-verify plan_epoch == session.epoch and re-resolve role
        #      fresh. If the epoch moved between (1) and (2), DROP the sends entirely: the reconcile
        #      that already landed only ever targeted the deck that WAS standby at check (1), and
        #      since we verified the epoch right before starting it, a toggle observed at check (2)
        #      means THAT deck is now LIVE. Pointing/recreating its tab or re-arming its boundary now
        #      would clobber the live deck mid-playback, and (for deck-navigate) would carry a role
        #      baked at plan time that now targets the wrong tab. Dropping is safe and sufficient: the
        #      reconcile itself is benign (it pushed the same vids the standby was about to receive;
        #      nothing user-visible changed), and the new standby (the deck that just went live) gets
        #      its own correct rebuild_standby + apply on the very next on_play/toggle cycle, so
        #      nothing is left stale. Best-effort; never raises into the WS loop.
        try:
            if plan is None:
                return
            with session.lock:
                if plan.get("epoch") is not None and plan["epoch"] != session.epoch:
                    logger.debug("dropping stale deck plan (plan epoch %r, session epoch %r)",
                                plan.get("epoch"), session.epoch)
                    return                      # stale: a toggle intervened before we even started
                label = plan["playlist_key"]
                deck = session.decks[label]
                ytm = deck["playlist_ytm"]
            if not ytm:
                return
            _ident, client = _radio_client()
            if client is not None:
                executor._reconcile(client, ytm, plan["vids"])
                with session.lock:
                    deck["applied_vids"] = list(plan["vids"])
            with session.lock:
                if plan.get("epoch") is not None and plan["epoch"] != session.epoch:
                    logger.debug("dropping deck sends: epoch moved mid-reconcile (plan epoch %r, "
                                "session epoch %r)", plan.get("epoch"), session.epoch)
                    return                      # a toggle landed while the reconcile was in flight
                role = "live" if label == session.live_label else "standby"
                epoch = session.epoch
            if plan.get("first"):
                bridge.send_control({"type": "deck-navigate", "role": role,
                                     "url": radio_mod.playlist_watch_url(plan["first"]["video_id"], ytm)})
            if plan.get("boundary"):
                bridge.send_control({"type": "deck-boundary-config", "role": role,
                                     "videoId": plan["boundary"], "epoch": epoch})
        except Exception:  # noqa: BLE001
            logger.warning("deck apply failed", exc_info=True)

    def _deck_rebuild_standby_apply(session, now):
        plan = radio_mod.rebuild_standby(getattr(ctx, "store", None), session, now)
        _deck_reconcile_navigate(session, plan)

    def _v2_tail_refresh_apply(session, now):
        # v2 steer reactivity (visibility wave, Part 3): the same tail-refresh /radio/populate's v2
        # body performs (force_topup's compute, then _radio_apply's reconcile), packaged as one pool
        # job so /radio/steer can fire-and-forget it exactly like the dual branch does via
        # `_deck_rebuild_standby_apply` above -- same shape, same single-worker pool, same "never
        # awaited from the route" rule.
        plan = radio_mod.force_topup(getattr(ctx, "store", None), session, now)
        _radio_apply(session, plan)

    def _dual_fallback_start(session, fallback_gen, now):
        # H3 (final review): a gen-matching deck-ready {fallback:true} while the session is active
        # means the extension could not stand up (or keep) the dual-deck window, and every one of its
        # degradation paths tears the radio window down. Flipping dual_deck alone left the session
        # with no queue, no prime, and no tab: silence with the UI saying "playing". So actually
        # START the v2 single-tab session here (seed queue, resolve the v2 playlist, navigate +
        # prime), mirroring /radio/start's v2 body. Runs on the single-worker apply pool, never
        # awaited from the WS receive loop (the reconcile below is a bridge round-trip that loop must
        # keep pumping). On any failure it is HONEST: reset + radio_active "0" so the UI shows radio
        # off and the owner can restart, instead of a swallowed failure surfaced as success. Never
        # raises into the pool.
        store = getattr(ctx, "store", None)
        try:
            identity_id, client = _radio_client()
            with session.lock:
                # Double-recheck at run time (same pattern as _deck_reconcile_navigate): a stop or a
                # newer /radio/start attempt landed between the WS frame and this pool slot -> drop.
                # A populated queue means a v2 session is already running (duplicate fallback frame,
                # e.g. a failed toggle's deck-ready plus the window's own onRemoved) -> inert.
                if not session.active or session.start_gen != fallback_gen or session.queue:
                    return
                plan = radio_mod.start_session(store, session, now) if store is not None else None
            if plan is None or client is None or store is None:
                # Nothing pickable / no client: be honest, the UI must not claim radio is playing.
                with session.lock:
                    session.reset()
                if store is not None:
                    store.set_setting("radio_active", "0")
                return
            ytm = store.get_setting("radio_playlist_ytm") or ""
            exists = any(p.ytm_playlist_id == ytm for p in store.get_playlists()) if ytm else False
            if not exists:
                res = executor.create_generated_playlist(
                    store, radio_mod.RADIO_PLAYLIST_TITLE,
                    [{"video_id": v} for v in plan["seed_vids"]], client, now, identity_id)
                ytm = res["new_ytm"]
                store.set_setting("radio_playlist_ytm", ytm)
            else:
                executor._reconcile(client, ytm, plan["seed_vids"])
            with session.lock:
                if not session.active or session.start_gen != fallback_gen:
                    return                      # stop/restart landed mid-reconcile: drop the sends
                session.playlist_ytm = ytm
                session.applied_vids = list(plan["seed_vids"])
            bridge.send_control({"type": "navigate",
                                 "url": radio_mod.playlist_watch_url(plan["first"]["video_id"], ytm)})
            if plan["primed"]:
                bridge.send_control({"type": "radio-prime",
                                     "url": radio_mod.playlist_watch_url(plan["primed"]["video_id"], ytm),
                                     "videoId": plan["primed"]["video_id"]})
        except Exception:  # noqa: BLE001 - fail HONESTLY, never raise into the pool
            logger.warning("radio v2 fallback start failed", exc_info=True)
            try:
                with session.lock:
                    session.reset()
                if store is not None:
                    store.set_setting("radio_active", "0")
            except Exception:  # noqa: BLE001
                pass

    @router.post("/radio/start")
    async def radio_start():
        radio = getattr(ctx, "radio", None)
        store = getattr(ctx, "store", None)
        identity_id, client = _radio_client()
        if radio is None or store is None or not bridge.connected or client is None:
            return {"ok": False, "reason": "not available"}
        now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()

        # Per-attempt generation stamp (T7i carried fix, T7h review): a stale/delayed deck-ready from an
        # earlier /radio/start attempt must never be allowed to flip a LATER session's dual_deck. Bump
        # once per attempt, under the lock, before anything else this call does; the dual path below
        # carries it in deck-start's "gen", and the WS deck-ready handler only accepts an echo that
        # matches session.start_gen at receipt time.
        with radio.lock:
            radio.start_gen += 1
            start_gen = radio.start_gen
            radio.fallback_reason = None   # honest slate for THIS attempt; see either failure path below

        # DUAL attempt first (spec V3): seed both decks off-loop, resolve/create both mini playlists
        # off-loop, then send ONE deck-start frame carrying both watch urls + the live deck's boundary
        # vid -- this is the frame that makes the extension create the radio window and both deck tabs.
        # Fails open to the v2 single-tab path (the `except` below, body unchanged from pre-dual radio)
        # on an empty dual seed or any error raised while resolving/creating the deck playlists.
        #
        # INTERIM dual_deck semantics (BINDING CORRECTION, T7h): start_dual_session sets
        # session.dual_deck True provisionally (see its own docstring), but the extension has not
        # confirmed the radio window + both deck tabs actually exist yet -- that confirmation is the
        # inbound "deck-ready" frame (see the WS handler below), whose handler is what should decide
        # dual_deck, not the seed. So it is downgraded back to False here, right after seeding, and
        # left there until deck-ready lands (or a reported fallback keeps it False for good).
        #
        # What that means for a `play` frame arriving in the window between this send and deck-ready:
        # on_play keys on session.dual_deck to pick its branch, so while it reads False here a play
        # frame is routed to on_play's v2 branch, which looks `vid` up in session.queue -- but
        # start_dual_session never populates session.queue (only session.decks), so that lookup always
        # misses and on_play's existing "user navigated off our queue: do not fight" no-op fires
        # (desired_vids/prime both None). Nothing is rebuilt, reconciled, or sent for that frame: it is
        # DROPPED, not routed through a working v2 session. This is safe and intentional, not a gap: the
        # decks themselves are fully real and audibly playable the instant deck-start lands (their mini
        # playlists already hold the seeded vids, and the extension can navigate/play either tab on its
        # own), only the SESSION's reactive bookkeeping (toggle-arming, standby rebuilds) is paused until
        # the extension confirms both tabs are really there. Pinned by
        # test_start_dual_play_frame_is_inert_before_deck_ready.
        def _seed_dual():
            with radio.lock:
                return radio_mod.start_dual_session(store, radio, now)
        dplan = await asyncio.to_thread(_seed_dual)
        try:
            if dplan is None:
                raise RuntimeError("dual seed empty")   # -> single-tab fallback in the except below

            def _resolve_deck_playlist(label, vids):
                key = radio_mod.RADIO_DECK_SETTING[label]
                ytm = store.get_setting(key) or ""
                exists = any(p.ytm_playlist_id == ytm for p in store.get_playlists()) if ytm else False
                if not exists:
                    res = executor.create_generated_playlist(
                        store, radio_mod.RADIO_DECK_TITLE[label],
                        [{"video_id": v} for v in vids], client, now, identity_id)
                    ytm = res["new_ytm"]
                    store.set_setting(key, ytm)
                else:
                    executor._reconcile(client, ytm, vids)
                return ytm

            def _setup():
                out = {}
                for side in ("live", "standby"):
                    p = dplan[side]
                    out[side] = _resolve_deck_playlist(p["playlist_key"], p["vids"])
                return out
            ytms = await asyncio.to_thread(_setup)
            with radio.lock:
                # M5 (final review), stop-during-start guard: /radio/stop (reset) or a NEWER
                # /radio/start attempt can land while _setup's playlist create/reconcile round-trips
                # are in flight. Without this recheck the continuation would re-populate the freshly
                # reset session, flip radio_active back to "1", and send deck-start for a session the
                # backend no longer runs: a zombie radio window playing with nothing reacting (S2 is
                # dead once active is False) and the UI saying stopped. Recheck BOTH flags under the
                # lock after the awaits and abort the send entirely on a mismatch.
                if not radio.active or radio.start_gen != start_gen:
                    logger.info("radio dual start aborted: stopped or superseded during setup "
                                "(active %r, start_gen %r vs attempt %r)",
                                radio.active, radio.start_gen, start_gen)
                    return {"ok": False, "reason": "stopped during start"}
                for side in ("live", "standby"):
                    p = dplan[side]
                    d = radio.decks[p["playlist_key"]]
                    d["playlist_ytm"] = ytms[side]
                    d["applied_vids"] = list(p["vids"])
                radio.dual_deck = False   # await deck-ready's confirmation; see the long comment above
                deck_epoch = radio.epoch
            store.set_setting("radio_active", "1")
            live_ytm, standby_ytm = ytms["live"], ytms["standby"]
            # C1 (final review): deck-start carries the session epoch so the extension can seed its
            # deck-toggled echo (lastDeckEpoch) from it. Without this, a skip-free first session (no
            # rebuild ever sends deck-boundary-config) acks its first natural toggle with epoch:null,
            # which the strict echo guard in the WS handler below rightly drops -- desyncing physical
            # tabs from logical decks at the very first boundary. C2: it also carries the STANDBY
            # deck's boundary vid, so the extension can arm (and re-arm across navigations) the
            # promoted deck's boundary instead of leaving its last-track end to YTM autoplay.
            bridge.send_control({
                "type": "deck-start",
                "liveUrl": radio_mod.playlist_watch_url(dplan["live"]["first"]["video_id"], live_ytm),
                "standbyUrl": radio_mod.playlist_watch_url(dplan["standby"]["first"]["video_id"], standby_ytm),
                "boundaryVideoId": dplan["live"]["boundary"],
                "standbyBoundaryVideoId": dplan["standby"]["boundary"],
                "epoch": deck_epoch, "gen": start_gen})
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - dual failed; fall back to the v2 single-tab start
            logger.warning("radio dual start failed, falling back to single tab", exc_info=True)
            # Fallback diagnostics, server side (the extension is not the only side that can fall
            # back to v2): stamp a reason so /bridge/status can tell the owner WHY, mirroring the
            # extension's own deck-ready {fallback:true, reason} path below. Best-effort: a stop
            # landing concurrently may have already reset the session, in which case this is harmless
            # (reset() clears it right back to None on the very next start attempt anyway).
            with radio.lock:
                radio.fallback_reason = f"server: {type(e).__name__}: {e}"

            # v2 single-tab start (pre-dual radio, unchanged). Seed the session (pure w.r.t. network,
            # but the picker runs radio_seed_depth scoring passes, which is CPU-bound): run off the
            # event loop so it never stalls other requests or the WS loop while this route awaits it.
            # Fail-open if nothing is pickable.
            def _seed():
                with radio.lock:
                    return radio_mod.start_session(store, radio, now)
            plan = await asyncio.to_thread(_seed)
            if plan is None:
                return {"ok": False, "reason": "not available"}
            try:
                # Resolve (create if needed) the app-managed playlist, then set it to exactly the seeds.
                def _seed_playlist():
                    ytm = store.get_setting("radio_playlist_ytm") or ""
                    exists = any(p.ytm_playlist_id == ytm for p in store.get_playlists()) if ytm else False
                    if not exists:
                        res = executor.create_generated_playlist(
                            store, radio_mod.RADIO_PLAYLIST_TITLE,
                            [{"video_id": v} for v in plan["seed_vids"]], client, now, identity_id)
                        ytm = res["new_ytm"]
                        store.set_setting("radio_playlist_ytm", ytm)
                    else:
                        executor._reconcile(client, ytm, plan["seed_vids"])
                    return ytm
                ytm = await asyncio.to_thread(_seed_playlist)
                with radio.lock:
                    radio.playlist_ytm = ytm
                    radio.applied_vids = list(plan["seed_vids"])
                store.set_setting("radio_active", "1")
                bridge.send_control({"type": "navigate",
                                     "url": radio_mod.playlist_watch_url(plan["first"]["video_id"], ytm)})
                if plan["primed"]:
                    bridge.send_control({"type": "radio-prime",
                                         "url": radio_mod.playlist_watch_url(plan["primed"]["video_id"], ytm),
                                         "videoId": plan["primed"]["video_id"]})
                return {"ok": True}
            except Exception:  # noqa: BLE001 - fail-open: nothing user-facing breaks
                logger.warning("radio start failed", exc_info=True)
                with radio.lock:
                    radio.reset()
                store.set_setting("radio_active", "0")
                return {"ok": False, "reason": "not available"}

    @router.post("/radio/stop")
    def radio_stop():
        radio = getattr(ctx, "radio", None)
        if radio is not None:
            with radio.lock:
                radio.reset()
        store = getattr(ctx, "store", None)
        if store is not None:
            store.set_setting("radio_active", "0")
        # #93 a stopped session must not leave a stale prime sitting in the extension: an organic
        # track end would otherwise hijack the tab back into the radio it just left. Best-effort:
        # the extension may already be gone.
        try:
            bridge.send_control({"type": "radio-prime", "url": None, "videoId": None})
        except Exception:  # noqa: BLE001 - stop must succeed even if the control send fails
            logger.warning("radio-prime clear send failed", exc_info=True)
        # Dual-deck teardown (T7i): tell the extension to normalize whichever deck survives (unmute the
        # live tab, clear its boundary), close the app-created tab, and blank (never close) the user's
        # original tab if it is the standby. Idempotent on the extension side even with no deck session
        # active. Best-effort, same as the radio-prime clear above.
        try:
            bridge.send_control({"type": "deck-stop"})
        except Exception:  # noqa: BLE001 - stop must succeed even if the control send fails
            logger.warning("deck-stop send failed", exc_info=True)
        return {"ok": True}

    @router.post("/radio/populate")
    async def radio_populate():
        # Testing/maintenance affordance (owner-requested): force a tail top-up right now, without
        # waiting for a track boundary, and apply it to the live playlist. Route context, so waiting
        # on the apply is safe here: the WS loop keeps pumping the reconcile's own bridge traffic.
        radio = getattr(ctx, "radio", None)
        store = getattr(ctx, "store", None)
        if radio is None or store is None or not getattr(radio, "active", False):
            return {"ok": False, "reason": "radio not active"}
        now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()
        if getattr(radio, "dual_deck", False):
            # DUAL mode: the button forces a rebuild_standby apply (rebuild the invisible standby
            # now) instead of a live-tail top-up -- there is no live tail to top up here, the live
            # deck's queue is a frozen snapshot until the next toggle (see _on_play_dual). Same
            # split as the WS deck-toggled branch: compute off-loop (CPU-bound picker, no network),
            # then hand the plan to the SAME apply pool _deck_reconcile_navigate runs on for every
            # other deck apply, so this is never a second, competing writer of deck state. Awaiting
            # the pool future here (unlike the WS receive loop) is safe: this is route context, not
            # the loop whose own reconcile round-trips would deadlock against it.
            plan = await asyncio.to_thread(radio_mod.rebuild_standby, store, radio, now)
            await asyncio.wrap_future(_radio_apply_pool.submit(_deck_reconcile_navigate, radio, plan))
            with radio.lock:
                standby_vids = [q["video_id"] for q in radio.standby["queue"]]
            return {"ok": True, "standby": standby_vids, "changed": plan is not None}
        plan = await asyncio.to_thread(radio_mod.force_topup, store, radio, now)
        await asyncio.wrap_future(_radio_apply_pool.submit(_radio_apply, radio, plan))
        with radio.lock:
            queue = [q["video_id"] for q in radio.queue]
            ahead = max(len(queue) - (radio.pos + 1), 0)
        return {"ok": True, "queue": queue, "ahead": ahead,
                "changed": plan.get("desired_vids") is not None}

    def _radio_customize_ctx():
        # The fingerprint supplies the genre/era axis inventory (what bars to show); tilts supplies
        # each slider's current value (default 1.0, neutral). Read-only: never touches rec_weights.
        store = getattr(ctx, "store", None)
        now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()
        fingerprint = recommend.taste_fingerprint(store, now) if store is not None else {"families": [], "eras": []}
        radio = getattr(ctx, "radio", None)
        return {"fingerprint": fingerprint, "tilts": (getattr(radio, "tilts", None) or {})}

    def _steer_rebuild_if_live_dual(radio):
        # #93 fix 3 (dual) + visibility wave Part 3 (v2): a tilt write alone is inaudible until the
        # next natural pick (a skip, a toggle, or an on_play tail rebuild) happens to react to it -- so
        # any successful steer/reset while a session is active submits the SAME reactive rebuild each
        # mode already runs on its own cadence: DUAL submits the standby-rebuild job the WS
        # deck-toggled branch submits (`_deck_rebuild_standby_apply`); v2 submits the same tail-refresh
        # /radio/populate's v2 body performs (`_v2_tail_refresh_apply`, force_topup + apply). Both are
        # fire-and-forget, NOT awaited: this is route context (safe to await in principle, unlike the
        # WS loop), but the route must still return the re-rendered bars immediately rather than block
        # on a re-pick, and the pool's single worker + each apply's own epoch/staleness guards already
        # serialize and drop anything stale. Several rapid slider drags each submit their own job
        # (FIFO): acceptable, each is just a cheap re-pick. This is what makes both modes' "Up next"
        # list (/bridge/status's radio_upcoming, Part 2) visibly reorder within a poll or two of a tilt
        # tweak -- previously v2 tilts only ever applied at the next natural pick.
        if radio is None or not getattr(radio, "active", False):
            return
        now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()
        if getattr(radio, "dual_deck", False):
            _radio_apply_pool.submit(_deck_rebuild_standby_apply, radio, now)
        else:
            _radio_apply_pool.submit(_v2_tail_refresh_apply, radio, now)

    @router.post("/radio/steer")
    async def radio_steer(request: Request):
        # #93 Task 9: drag a genre/era tilt bar in the radio customize panel -> set a SESSION-ONLY
        # tilt (ctx.radio.tilts), never a standing lean/rec_weight. Mirrors /home/steer's shape but
        # writes to the radio session instead of the permanent taste model. Fail-open: a malformed
        # or out-of-range post is a no-op, never a 500, and always returns the re-rendered bars.
        form = await request.form()
        axis, weight = (form.get("axis") or "").strip(), form_float(form.get("weight"))
        radio = getattr(ctx, "radio", None)
        if radio is not None and axis and weight is not None and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX
            with radio.lock:
                radio.tilts[axis] = max(lo, min(hi, weight))
            _steer_rebuild_if_live_dual(radio)
        return templates.TemplateResponse(request, "_partials/radio_customize.html", _radio_customize_ctx())

    @router.post("/radio/steer/reset")
    def radio_steer_reset(request: Request):
        radio = getattr(ctx, "radio", None)
        if radio is not None:
            with radio.lock:
                radio.tilts = {}
            _steer_rebuild_if_live_dual(radio)
        return templates.TemplateResponse(request, "_partials/radio_customize.html", _radio_customize_ctx())

    @router.post("/play")
    async def play(request: Request):
        # Play a YouTube Music URL by swapping the existing YTM tab (in the background) via the
        # extension, instead of opening a new tab. Any app play link routes through here.
        try:
            url = (await request.json()).get("url") or ""
        except Exception:  # noqa: BLE001
            url = ""
        if not url.startswith("https://music.youtube.com/"):
            return {"ok": False, "error": "url must be a music.youtube.com link"}
        try:
            bridge.send_control({"type": "navigate", "url": url})
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - extension not connected; UI falls back to opening it
            return {"ok": False, "error": str(e)}

    @router.post("/now-playing/rate")
    async def now_playing_rate(request: Request):
        # Like/dislike the currently-playing track by asking the extension to drive YTM's own control.
        try:
            action = (await request.json()).get("action")
        except Exception:  # noqa: BLE001
            action = None
        if action not in ("like", "dislike"):
            return {"ok": False, "error": "action must be 'like' or 'dislike'"}
        try:
            bridge.send_control({"type": "rate", "action": action})
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - extension not connected, surface it to the UI
            return {"ok": False, "error": str(e)}

    @router.post("/now-playing/toggle")
    async def now_playing_toggle():
        # Play/pause the currently-playing track by asking the extension to drive YTM's own control.
        try:
            bridge.send_control({"type": "playpause"})
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - extension not connected, surface it to the UI
            return {"ok": False, "error": str(e)}

    @router.websocket("/bridge/ws")
    async def bridge_ws(ws: WebSocket):
        # Origin is set by the browser and unspoofable by page scripts, so it is a sound gate.
        if ws.headers.get("origin") not in EXTENSION_ORIGINS:
            logger.warning("bridge handshake rejected (origin %r)", ws.headers.get("origin"))
            await ws.close(code=1008)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()

        async def send(frame):
            # Serialize sends: the bridge request path and the keepalive pinger both write here.
            async with send_lock:
                await ws.send_json(frame)

        async def keepalive():
            # An MV3 service worker sleeps after ~30s idle, which drops this socket (and then every
            # backend write fails with "no extension connected"). Incoming message activity resets
            # that timer, so ping every 20s to keep the extension and the pipe alive.
            try:
                while True:
                    await asyncio.sleep(20)
                    async with send_lock:
                        await ws.send_json({"ping": 1})
            except Exception:  # noqa: BLE001 - a closing socket races the sleep; disconnect handles it
                pass

        conn_id = bridge.connect(send, loop)
        ping_task = asyncio.create_task(keepalive())
        # A live pairing is the credential now (see Runtime.credentials_present), so record it as
        # soon as our extension connects. Guard for a store being present since some tests build a
        # bare ctx without one.
        store = getattr(ctx, "store", None)
        if store is not None:
            store.set_setting("bridge_paired", "1")
        # A live extension means a live session, so clear any stale "not signed in" flags (they may
        # have been set earlier when the extension was merely disconnected). A genuine signed-out
        # state re-flags on the next sync attempt. Guard: some tests build a bare ctx.
        clear = getattr(ctx, "clear_all_auth_expired", None)
        if callable(clear):
            clear()
        logger.info("extension bridge connected")
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "deck-ready":
                    # The extension confirms both decks exist (or reports a fallback to one tab).
                    # A false report downgrades dual_deck back to the v2 single-tab path (T7h).
                    #
                    # Gen-guarded (T7i carried fix, T7h review): a stale/delayed deck-ready from an
                    # earlier /radio/start attempt must not flip a LATER session's dual_deck. Every
                    # deck-start we send carries the attempt's "gen" (session.start_gen at send time);
                    # this frame is accepted only when its echoed "gen" matches session.start_gen RIGHT
                    # NOW. A stale echo (an older attempt) or an absent one (pre-contract frame) is
                    # dropped with a debug log, not applied.
                    radio = getattr(ctx, "radio", None)
                    if radio is not None:
                        fallback_gen = None
                        with radio.lock:
                            echoed, current_gen = msg.get("gen"), radio.start_gen
                            if echoed == current_gen:
                                radio.dual_deck = not bool(msg.get("fallback"))
                                # Fallback diagnostics (visibility wave): a confirmed fallback stores
                                # WHY (the extension's own reason string, see sendDeckReady) so
                                # /bridge/status can surface it instead of the owner seeing nothing but
                                # "not dual" with no explanation. A confirmed (non-fallback) deck-ready
                                # is dual actually working, so any earlier fallback reason (a previous
                                # attempt's, or the server-side one from /radio/start's except path) is
                                # stale and cleared.
                                if bool(msg.get("fallback")):
                                    reason = msg.get("reason")
                                    radio.fallback_reason = reason
                                    logger.warning("radio dual fell back: %s", reason)
                                else:
                                    radio.fallback_reason = None
                                # H3 (final review): a confirmed fallback while active must actually
                                # START the v2 single-tab session (queue + prime + navigate), not just
                                # flip the flag -- the extension tears the radio window down on every
                                # degradation path, so flag-only "fallback" is silence with the UI
                                # saying playing. The YTM work runs on the single-worker apply pool
                                # like every other deck apply, NEVER awaited here (WS loop rule). The
                                # queue-empty gate keeps a duplicate fallback frame inert once the v2
                                # session is up (the job itself rechecks under the lock too).
                                if bool(msg.get("fallback")) and radio.active and not radio.queue:
                                    fallback_gen = current_gen
                            else:
                                logger.debug("dropping stale/absent deck-ready (echoed gen %r, "
                                            "session start_gen %r)", echoed, current_gen)
                        if fallback_gen is not None:
                            now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()
                            _radio_apply_pool.submit(_dual_fallback_start, radio, fallback_gen, now)
                    continue
                if isinstance(msg, dict) and msg.get("type") == "deck-toggled":
                    # The extension confirms a swap actually happened: fold the toggle into session
                    # state, then schedule the new standby's rebuild off the WS loop (apply pool).
                    #
                    # DEFECT 2 (T7g review): a duplicate delivery of the SAME confirmed swap (extension
                    # retry, or a redelivered frame) must not fold the toggle in twice -- that would
                    # flip live_label straight back and desync everything downstream. Guarded with an
                    # epoch echo: every deck-boundary-config/deck-toggle frame we send carries the
                    # session epoch at send time, and the extension echoes it back here verbatim. The
                    # FIRST genuine deck-toggled's echoed epoch equals session.epoch (nothing has
                    # bumped it yet); toggle_decks then bumps it. A DUPLICATE of that same confirmation
                    # echoes the same (now stale) epoch, which no longer matches session.epoch, so it
                    # is dropped instead of toggling again. An UNTAGGED frame (no "epoch" key at all) is
                    # also dropped, with a warning: the extension side (T7j/T7k) always carries the
                    # echo -- seeded at deck-start (C1, final review), updated by every
                    # deck-boundary-config/deck-toggle, and persisted across SW restarts -- so nothing
                    # legitimate can arrive without it.
                    radio = getattr(ctx, "radio", None)
                    if radio is not None and getattr(radio, "active", False):
                        echoed = msg.get("epoch")
                        if echoed is None:
                            logger.warning("deck-toggled missing epoch echo, ignoring: %r", msg)
                            continue
                        with radio.lock:
                            current_epoch = radio.epoch
                        if echoed != current_epoch:
                            logger.debug("dropping stale/duplicate deck-toggled (echoed epoch %r, "
                                        "session epoch %r)", echoed, current_epoch)
                            continue
                        now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()
                        radio_mod.toggle_decks(radio)
                        _radio_apply_pool.submit(_deck_rebuild_standby_apply, radio, now)
                    continue
                # The extension can push unsolicited events (not replies to a request). A play
                # notification carries what is currently playing in the YouTube Music tab.
                if isinstance(msg, dict) and msg.get("type") == "play":
                    if msg.get("deck") == "standby":
                        continue   # a muted, paused standby deck must never register a play or flip the bar
                    logger.info("Received play notification: %s by %s",
                                msg.get("title") or "?", msg.get("artist") or "?")
                    # Waiting-state net: a real (non-standby) play frame is the confirmation that
                    # whatever "deck-waiting" episode was pending has resolved, one way or another
                    # (the retry listener firing, or the owner otherwise getting it playing).
                    radio = getattr(ctx, "radio", None)
                    if radio is not None:
                        with radio.lock:
                            radio.waiting = False
                    # Surface it for the Home now-playing line (polled via GET /bridge/status).
                    bridge.now_playing = {"title": msg.get("title"), "artist": msg.get("artist"),
                                          "thumbnail": msg.get("thumbnail"),
                                          "likeStatus": msg.get("likeStatus"),
                                          "video_id": msg.get("videoId"),
                                          "paused": bool(msg.get("paused"))}
                    # #75 persist it: play_events + the (track, day) model + freshness stamp.
                    # Store calls block, so run off the event loop; a bad frame or a stub store
                    # (tests) must never kill the bridge socket.
                    if store is not None:
                        now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()
                        try:
                            await asyncio.to_thread(live_plays.handle_play_event, ctx, msg, now)
                        except Exception:  # noqa: BLE001
                            logger.warning("failed to persist play event", exc_info=True)
                        # #93 dynamic radio advances the queue (and tops it up) off the loop; it must
                        # never raise into the socket, matching the persist above.
                        radio = getattr(ctx, "radio", None)
                        if radio is not None and getattr(radio, "active", False):
                            try:
                                out = await asyncio.to_thread(radio_mod.on_play, store, radio,
                                                              msg.get("videoId"), now)
                                # Fire-and-forget: see _radio_apply_pool's comment (awaiting here
                                # deadlocks the socket against its own reconcile round-trips).
                                if "at_boundary" in out:            # DUAL plan
                                    if out.get("foreign"):
                                        # S2 fail-safe: on_play gates `foreign` to sessions that were
                                        # already sitting at the boundary (T7d/T7e); trust that gate
                                        # here, do not re-derive it. M4 (final review): additionally
                                        # require the frame to have come from the LIVE deck tab (the
                                        # extension's sender-based tag) -- the user playing a song in
                                        # their OWN YTM tab while the live deck sits on its boundary
                                        # track would otherwise fire a spurious deck swap. Untagged
                                        # ("unknown") frames keep v2's do-not-fight inertness. Carry
                                        # the current epoch (DEFECT 2, T7g review): the extension
                                        # echoes it back in deck-toggled so a duplicate confirmation
                                        # can be told apart from the real one.
                                        if msg.get("deck") == "live":
                                            with radio.lock:
                                                deck_epoch = radio.epoch
                                            bridge.send_control({"type": "deck-toggle",
                                                                 "epoch": deck_epoch})
                                        else:
                                            logger.debug("ignoring foreign play from non-live deck %r",
                                                        msg.get("deck"))
                                    elif out.get("at_boundary") and out.get("standby_dirty"):
                                        _radio_apply_pool.submit(_deck_rebuild_standby_apply, radio, now)
                                else:                                # v2 single-tab plan
                                    _radio_apply_pool.submit(_radio_apply, radio, out)
                            except Exception:  # noqa: BLE001
                                logger.warning("radio on_play failed", exc_info=True)
                    continue
                if isinstance(msg, dict) and msg.get("type") == "pevent":
                    # ATTRIBUTION SUBTLETY (waiting-state net): a "deck-waiting" pevent is content.js's
                    # report that a deck-play attempt's play() was rejected. That deck-play is sent to
                    # the about-to-be-promoted tab BEFORE toggleDecks swaps liveTabId/standbyTabId (see
                    # background.js's pevent-relay comment), and the rejection report is async, so the
                    # SAME waiting episode can race that swap and arrive tagged either "standby" or
                    # "live". deck-waiting only ever originates from a deck-play attempt, which only
                    # ever targets the promoted-or-live tab, so it is handled here BEFORE the
                    # standby-drop guard below, regardless of which tag it happened to race into.
                    if msg.get("kind") == "deck-waiting":
                        radio = getattr(ctx, "radio", None)
                        if radio is not None:
                            with radio.lock:
                                radio.waiting = True
                        continue
                    if msg.get("deck") == "standby":
                        continue   # a muted, paused standby deck must never register a play or flip the bar
                    # #97 a "state" pevent tells us play/pause without waiting for a new "play" frame,
                    # and "bye" means the YTM tab is gone, so nothing is playing anymore. Update
                    # now_playing BEFORE persisting; never let a malformed frame raise into the socket.
                    try:
                        kind = msg.get("kind")
                        if kind == "state" and bridge.now_playing is not None:
                            # Defense-in-depth: a state event tagged with a videoId that is not the
                            # track the bar is showing (stale tag during a transition) must not flip
                            # the new track's paused state. No videoId on either side: apply as-is.
                            sv, nv = msg.get("videoId"), bridge.now_playing.get("video_id")
                            if not sv or not nv or sv == nv:
                                bridge.now_playing["paused"] = (msg.get("state") == "paused")
                        elif kind == "bye":
                            bridge.now_playing = None
                    except Exception:  # noqa: BLE001
                        logger.warning("failed to update now_playing from pevent", exc_info=True)
                    # #91 raw player/curation event; persist off the loop, never kill the socket.
                    # `now` is bound OUTSIDE the store guard (L8, final review): the radio react
                    # below runs regardless of `store`, and binding it only under the guard left a
                    # bare-ctx (test) path where react raised a swallowed NameError.
                    now = ctx.now_fn() if getattr(ctx, "now_fn", None) else time.time()
                    if store is not None:
                        try:
                            await asyncio.to_thread(player_events.handle_player_event, ctx, msg, now)
                        except Exception:  # noqa: BLE001
                            logger.warning("failed to persist player event", exc_info=True)
                    # #93 dynamic radio reacts to this event off the loop; it must never raise into
                    # the socket, matching the persist above.
                    radio = getattr(ctx, "radio", None)
                    if radio is not None and getattr(radio, "active", False):
                        try:
                            await asyncio.to_thread(radio_mod.react, store, radio, msg, now)
                        except Exception:  # noqa: BLE001
                            logger.warning("radio react failed", exc_info=True)
                    continue
                try:
                    req_id = int(msg["id"])
                    status = int(msg["status"])
                    body = msg["body"]
                except (KeyError, ValueError, TypeError):
                    logger.warning("malformed bridge frame, ignoring: %r", msg)
                    continue
                bridge.resolve(req_id, status, body)
        except WebSocketDisconnect:
            pass
        finally:
            ping_task.cancel()
            bridge.disconnect(conn_id)
            bridge.now_playing = None      # nothing is playing once the extension is gone
            # #93 the WS dropping is the real "tab gone" signal (a pagehide "bye" is not: it also
            # fires on every in-tab navigation, including the radio's own). Reset the session here,
            # not on bye, so the radio does not kill itself on its own hard navigation.
            radio = getattr(ctx, "radio", None)
            if radio is not None:
                with radio.lock:
                    radio.reset()
            try:
                if store is not None:
                    store.set_setting("radio_active", "0")
            except Exception:  # noqa: BLE001 - best-effort, must never block the disconnect cleanup
                logger.warning("failed to clear radio_active on disconnect", exc_info=True)
            logger.info("extension bridge disconnected")

    return router
