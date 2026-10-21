"""App-owned radio orchestration. Socket handlers never wait for remote playlist writes."""
import asyncio
import logging
import time

from yt_playlist.core.activity import activity_for
from yt_playlist.library import executor, playlist_mutation
from yt_playlist.rec import radio as radio_mod

logger = logging.getLogger(__name__)


class RadioController:
    def __init__(self, ctx):
        self.ctx = ctx
        self.work = activity_for(ctx)
        self.bridge = ctx.bridge

    @property
    def session(self):
        return getattr(self.ctx, "radio", None)

    @property
    def store(self):
        return getattr(self.ctx, "store", None)

    def now(self):
        return self.ctx.now_fn() if getattr(self.ctx, "now_fn", None) else time.time()

    def client(self):
        return next(iter((self.ctx.client_provider() or {}).items()), (None, None))

    def _resolve(self, key, title, vids, client, identity, now):
        ytm = self.store.get_setting(key) or ""
        if not ytm or not any(p.ytm_playlist_id == ytm for p in self.store.get_playlists()):
            result = executor.create_generated_playlist(
                self.store, title, [{"video_id": v} for v in vids], client, now, identity)
            ytm = result["new_ytm"]
            self.store.set_setting(key, ytm)
        result = playlist_mutation.reconcile_playlist(client, ytm, vids)
        return ytm, result

    def _record(self, session, result, target=None):
        session.apply_result = result.diagnostics()
        if target is not None:
            target["apply_result"] = result.diagnostics()
            if result.confirmed_vids is not None:
                target["applied_vids"] = list(result.confirmed_vids)
        elif result.confirmed_vids is not None:
            session.applied_vids = list(result.confirmed_vids)

    def apply(self, session, plan, generation):
        with session.lock:
            if not session.active or session.start_gen != generation:
                return
            ytm = session.playlist_ytm
        if not ytm:
            return
        try:
            vids = plan.get("desired_vids")
            if vids is not None:
                _, client = self.client()
                result = (playlist_mutation.reconcile_playlist(client, ytm, vids) if client else
                          playlist_mutation.PlaylistResult("failed", None, error="not connected"))
                with session.lock:
                    if not session.active or session.start_gen != generation:
                        return
                    self._record(session, result)
                    if not result.complete:
                        return result
            with session.lock:
                if not session.active or session.start_gen != generation:
                    return
                prime = plan.get("prime")
                if prime:
                    self.bridge.send_control({"type": "radio-prime", "videoId": prime["video_id"],
                                              "url": radio_mod.playlist_watch_url(prime["video_id"], ytm)})
            return result if vids is not None else None
        except Exception as exc:
            logger.warning("radio apply failed", exc_info=True)
            result = playlist_mutation.PlaylistResult("failed", None, error=str(exc))
            with session.lock:
                if session.active and session.start_gen == generation:
                    self._record(session, result)
            return result

    def apply_deck(self, session, plan, generation):
        if plan is None:
            return
        with session.lock:
            if (not session.active or session.start_gen != generation
                    or plan.get("epoch", session.epoch) != session.epoch):
                return
            label = plan["playlist_key"]
            deck = session.decks[label]
            ytm = deck["playlist_ytm"]
        if not ytm:
            return
        try:
            _, client = self.client()
            result = (playlist_mutation.reconcile_playlist(client, ytm, plan["vids"]) if client else
                      playlist_mutation.PlaylistResult("failed", None, error="not connected"))
            with session.lock:
                if not session.active or session.start_gen != generation:
                    return
                self._record(session, result, deck)
                if not result.complete:
                    session.standby_dirty = True
                    return result
                # A toggle during the remote call makes this deck live. Retain confirmed state,
                # but never navigate/re-arm that newly live deck with an old standby plan.
                if plan.get("epoch", session.epoch) != session.epoch:
                    return result
                role = "live" if label == session.live_label else "standby"
                if plan.get("first"):
                    self.bridge.send_control({"type": "deck-navigate", "role": role,
                        "url": radio_mod.playlist_watch_url(plan["first"]["video_id"], ytm)})
                if plan.get("boundary"):
                    self.bridge.send_control({"type": "deck-boundary-config", "role": role,
                                              "videoId": plan["boundary"], "epoch": session.epoch})
            return result
        except Exception as exc:
            logger.warning("deck apply failed", exc_info=True)
            result = playlist_mutation.PlaylistResult("failed", None, error=str(exc))
            with session.lock:
                if session.active and session.start_gen == generation:
                    self._record(session, result, deck)
                    session.standby_dirty = True
            return result

    def refresh(self, session, now, generation, dual):
        with session.lock:
            if not session.active or session.start_gen != generation:
                return
            plan = (radio_mod.rebuild_standby(self.store, session, now) if dual else
                    radio_mod.force_topup(self.store, session, now))
        return (self.apply_deck if dual else self.apply)(session, plan, generation)

    def steer(self):
        session = self.session
        if session is not None:
            with session.lock:
                if session.active:
                    self.work.submit(self.refresh, session, self.now(), session.start_gen, session.dual_deck)

    def _seed_single(self, session, generation, now):
        with session.lock:
            if session.start_gen != generation:
                return None
            return radio_mod.start_session(self.store, session, now)

    def _start_single(self, session, generation, plan, now, client, identity):
        with session.lock:
            if not session.active or session.start_gen != generation:
                return {"ok": False, "reason": "stopped during start"}
        ytm, result = self._resolve(radio_mod.RADIO_PLAYLIST_SETTING, radio_mod.RADIO_PLAYLIST_TITLE,
                                    plan["seed_vids"], client, identity, now)
        with session.lock:
            if not session.active or session.start_gen != generation:
                return {"ok": False, "reason": "stopped during start"}
            session.playlist_ytm = ytm
            self._record(session, result)
            if not result.complete:
                session.active = False
                self.store.set_setting("radio_active", "0")
                return {"ok": False, "reason": result.error, "apply": result.diagnostics()}
            self.store.set_setting("radio_active", "1")
            self.bridge.send_control({"type": "navigate",
                "url": radio_mod.playlist_watch_url(plan["first"]["video_id"], ytm)})
            if plan["primed"]:
                self.bridge.send_control({"type": "radio-prime", "videoId": plan["primed"]["video_id"],
                    "url": radio_mod.playlist_watch_url(plan["primed"]["video_id"], ytm)})
        return {"ok": True}

    async def start(self):
        session, store = self.session, self.store
        identity, client = self.client()
        if session is None or store is None or client is None or not self.bridge.connected:
            return {"ok": False, "reason": "not available"}
        now = self.now()
        with session.lock:
            session.start_gen += 1
            generation = session.start_gen
            session.fallback_reason = None

        def setup_dual():
            with session.lock:
                if session.start_gen != generation:
                    return {"ok": False, "reason": "stopped during start"}
                plan = radio_mod.start_dual_session(store, session, now)
            if plan is None:
                raise RuntimeError("dual seed empty")
            resolved = {}
            for side in ("live", "standby"):
                part = plan[side]
                label = part["playlist_key"]
                ytm, result = self._resolve(radio_mod.RADIO_DECK_SETTING[label],
                    radio_mod.RADIO_DECK_TITLE[label], part["vids"], client, identity, now)
                with session.lock:
                    if not session.active or session.start_gen != generation:
                        return {"ok": False, "reason": "stopped during start"}
                    deck = session.decks[label]
                    deck["playlist_ytm"] = ytm
                    self._record(session, result, deck)
                    if not result.complete:
                        raise RuntimeError(result.error)
                resolved[side] = ytm
            with session.lock:
                if not session.active or session.start_gen != generation:
                    return {"ok": False, "reason": "stopped during start"}
                session.dual_deck = False  # extension confirms actual tabs with deck-ready
                store.set_setting("radio_active", "1")
                self.bridge.send_control({"type": "deck-start",
                    "liveUrl": radio_mod.playlist_watch_url(plan["live"]["first"]["video_id"], resolved["live"]),
                    "standbyUrl": radio_mod.playlist_watch_url(plan["standby"]["first"]["video_id"], resolved["standby"]),
                    "boundaryVideoId": plan["live"]["boundary"],
                    "standbyBoundaryVideoId": plan["standby"]["boundary"],
                    "epoch": session.epoch, "gen": generation})
            return {"ok": True}

        try:
            return await asyncio.wrap_future(self.work.submit(setup_dual))
        except Exception as exc:
            logger.warning("radio dual start failed, falling back to single tab", exc_info=True)
            with session.lock:
                if session.start_gen != generation:
                    return {"ok": False, "reason": "stopped during start"}
                session.fallback_reason = f"server: {type(exc).__name__}: {exc}"
            try:
                plan = await self.work.to_thread(self._seed_single, session, generation, now)
                if plan is None:
                    return {"ok": False, "reason": "not available"}
                return await asyncio.wrap_future(self.work.submit(
                    self._start_single, session, generation, plan, now, client, identity))
            except Exception:
                logger.warning("radio start failed", exc_info=True)
                self._fail(session, generation)
                return {"ok": False, "reason": "not available"}

    def _fail(self, session, generation):
        with session.lock:
            if session.start_gen == generation:
                session.reset()
                if self.store is not None:
                    self.store.set_setting("radio_active", "0")

    def stop(self, *, send=True):
        session = self.session
        if session is not None:
            with session.lock:
                session.start_gen += 1  # invalidates queued work, including plans with epoch zero
                session.reset()
        if self.store is not None:
            self.store.set_setting("radio_active", "0")
        if send:
            for frame in ({"type": "radio-prime", "url": None, "videoId": None}, {"type": "deck-stop"}):
                try:
                    self.bridge.send_control(frame)
                except Exception:
                    logger.warning("radio stop send failed", exc_info=True)
        return {"ok": True}

    async def populate(self):
        session = self.session
        if session is None or self.store is None or not session.active:
            return {"ok": False, "reason": "radio not active"}
        with session.lock:
            generation, dual = session.start_gen, session.dual_deck
        result = await asyncio.wrap_future(self.work.submit(self.refresh, session, self.now(), generation, dual))
        with session.lock:
            if not session.active or session.start_gen != generation:
                return {"ok": False, "reason": "stopped during populate"}
            response = {"ok": result is None or result.complete,
                        "changed": result is not None}
            if dual:
                response["standby"] = [p["video_id"] for p in session.standby["queue"]]
            else:
                response["queue"] = [p["video_id"] for p in session.queue]
                response["ahead"] = max(len(session.queue) - session.pos - 1, 0)
            if result is not None and not result.complete:
                response["apply"] = result.diagnostics()
            return response

    def deck_ready(self, message):
        session = self.session
        if session is None:
            return
        with session.lock:
            if message.get("gen") != session.start_gen:
                return
            fallback = bool(message.get("fallback"))
            session.dual_deck = not fallback
            session.fallback_reason = message.get("reason") if fallback else None
            if fallback and session.active and not session.queue:
                self.work.submit(self._fallback, session, session.start_gen, self.now())

    def _fallback(self, session, generation, now):
        try:
            with session.lock:
                if not session.active or session.start_gen != generation or session.queue:
                    return
                plan = self._seed_single(session, generation, now)
            identity, client = self.client()
            if plan is None or client is None:
                self._fail(session, generation)
                return
            self._start_single(session, generation, plan, now, client, identity)
        except Exception:
            logger.warning("radio fallback failed", exc_info=True)
            self._fail(session, generation)

    def deck_toggled(self, message):
        session = self.session
        if session is not None:
            with session.lock:
                if not session.active or message.get("epoch") != session.epoch:
                    return
                radio_mod.toggle_decks(session)
                self.work.submit(self.refresh, session, self.now(), session.start_gen, True)

    def played(self, message, now):
        session = self.session
        if session is None:
            return
        with session.lock:
            session.waiting = False
            if not session.active:
                return
            generation = session.start_gen
            plan = radio_mod.on_play(self.store, session, message.get("videoId"), now)
            if "at_boundary" not in plan:
                self.work.submit(self.apply, session, plan, generation)
            elif plan.get("foreign"):
                if message.get("deck") == "live":
                    self.bridge.send_control({"type": "deck-toggle", "epoch": session.epoch})
            elif plan.get("at_boundary") and plan.get("standby_dirty"):
                self.work.submit(self.refresh, session, now, generation, True)

    def reacted(self, message, now):
        session = self.session
        if session is not None:
            with session.lock:
                if message.get("kind") == "deck-waiting":
                    session.waiting = True
                elif session.active:
                    radio_mod.react(self.store, session, message, now)
