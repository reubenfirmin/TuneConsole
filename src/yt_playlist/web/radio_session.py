"""Saved-playlist radio, observed through the extension's native playback state.

Start with three songs in a new YouTube playlist; then keep one song ahead of
the currently playing song. Ask Music's native refresh handler to fetch the saved
playlist, without supplying playback items or reloading the tab.
"""
import json
import logging
import threading
import time
from datetime import date, datetime
from uuid import uuid4

from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_policy import RadioPolicy
from yt_playlist.rec import choice_evidence, radio_tuning
from yt_playlist.web.radio_edits import AppendOperation
from yt_playlist.web.radio_launch import RadioLaunch
from yt_playlist.web.radio_listening import RadioListening
from yt_playlist.web.radio_mutations import MutationResult, PlaylistTarget, RadioMutations
from yt_playlist.web.radio_playlist_tail import LOOKAHEAD, RadioPlaylistTail
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending, RadioPlaylistIO
from yt_playlist.web.radio_playback import RadioPlayback
from yt_playlist.web.radio_persistence import RadioPersistence
from yt_playlist.web.radio_preparation import RadioPreparation
from yt_playlist.web.radio_projection import status_view
from yt_playlist.web.radio_state import Phase

logger = logging.getLogger(__name__)


class PlaylistRadio:
    def __init__(self, ctx):
        self.ctx = ctx
        self.work = activity_for(ctx)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.session = None
        self.offer = None
        self.offer_id = None
        self.persistence = RadioPersistence(ctx.store.settings)
        self.playback = RadioPlayback(ctx.bridge)
        self.playlists = RadioPlaylistIO(ctx.store.playlists)
        self.mutations = RadioMutations(self.playlists, self.persistence, ctx.store.tracks, lambda: self.ctx.now_fn())
        self.policy = RadioPolicy(ctx.store, lambda: self.ctx.now_fn())
        self.listening = RadioListening(ctx.store, self.playback, self.policy, self.lock, lambda: self.ctx.now_fn())
        self.launch = RadioLaunch(ctx.store, self.playlists, self.persistence, self.policy,
                                  self.lock, lambda: self.ctx.now_fn())
        self.preparation = RadioPreparation(ctx.store, self.policy, self.work, lambda: self.ctx.now_fn())
        self.preparing = False
        self.checked_playlist = None

    def _playing(self):
        """The extension's fresh playback presence is authoritative, not saved state."""
        return self.playback.current()

    def _is_current(self, s):
        return self.listening.current(s)

    def prepare(self):
        """Schedule preparation without waiting on session work or browser replies."""
        # Playback heartbeats call this from the WebSocket response pump. A busy
        # session can retry on the next heartbeat; it must not hold up its own replies.
        if not self.lock.acquire(blocking=False):
            return
        try:
            playing = self._playing().get("playlist_ytm_id")
            settled = (self.session and (self.session.monitoring or self.session.stopped_by_user
                        or not self._is_current(self.session))) or (
                        self.session is None and playing == self.checked_playlist)
            if self.preparing or not self.work.accepting or (self.offer is not None and settled):
                return
            self.preparing = True
        finally:
            self.lock.release()
        def warm():
            try:
                self.join()
                self.checked_playlist = playing
                if self.offer is None:
                    self.seeds()
            except Exception:
                logger.debug("Radio preparation deferred", exc_info=True)
            finally:
                with self.lock:
                    self.preparing = False
        try:
            self.work.start_thread(warm, name="radio-prepare")
        except Exception:
            self.preparing = False
            raise

    def bootstrap(self):
        """Return the last good view immediately, with no browser or model work."""
        state = self.status() if self.session is not None else self.launch.checkpoint_view(self._playing())
        try:
            offer = json.loads(self.ctx.store.settings.get_setting("radio_seed_offer") or "{}")
        except (ValueError, TypeError):
            offer = {}
        return {"state": state, "seeds": self.offer if self.offer is not None else offer.get("tracks", []),
                "seedMenu": self.offer_id or offer.get("menu_id"),
                "playback": {"connected": self.ctx.bridge.connected, "now_playing": self._playing() or None}}

    def _persist(self, s):
        """Keep a resumable pointer using the existing settings API."""
        if s is not self.session or not s.playlist or not s.local_id:
            return
        with self.lock:
            if s is not self.session:
                return
            s.settle()
            self.persistence.save(s)

    def join(self):
        """Resume only owned playback; never navigate from a restored checkpoint."""
        with self.lock:
            s = self.session
            if s and (s.monitoring or s.stopped_by_user or not self.ctx.bridge.connected):
                return self.status()
        with self.operation:
            if self.session is None:
                playing, clients = self._playing(), self.ctx.client_provider() or {}
                try:
                    state = self.persistence.load()
                    if state and state.get("stopped") and state.get("playlist") == playing.get("playlist_ytm_id"):
                        return self.status()
                    s, changed = self.launch.resume(playing, clients, RadioCandidates)
                    self.session = s
                    if s and s.tracks:
                        self.listening.update(s, force=not s.replacement and changed)
                except (ValueError, KeyError, TypeError):
                    logger.warning("Could not restore the saved Radio Session", exc_info=True)
                if self.session is None:
                    self.session = s = self.launch.adopt(playing, clients, RadioCandidates)
                    if s:
                        self._mirror(s, s.tracks)
                        self.listening.update(s)
                        self._persist(s)
            s = self.session
            if (s and not s.stopped_by_user and not s.monitoring
                    and self._is_current(s) and self.ctx.bridge.connected and self.work.accepting):
                s.stop = threading.Event()
                self._start_monitor(s)
        return self.status()

    def _start_monitor(self, s):
        s.monitoring = True
        try:
            self.work.start_thread(self._monitor, args=(s,), name="radio-session")
        except Exception:
            s.monitoring = False
            raise

    def seeds(self):
        with self.operation:
            if self.offer is None:
                candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn())
                self.offer, self.offer_id = self.listening.seed_offer(candidates)
            return {"tracks": self.offer, "menu_id": self.offer_id}

    @property
    def active(self):
        with self.lock:
            return bool(self._is_current(self.session))

    def status(self):
        with self.lock:
            s = self.session
            return status_view(s, active=bool(self._is_current(s)),
                               connected=self.ctx.bridge.connected, now=time.monotonic())

    def stop(self):
        with self.lock:
            if self.session:
                self.session.request_stop()
                if not self.session.monitoring:
                    self.session.move(Phase.STOPPED)
                self._persist(self.session)
        return {"ok": True, **self.status()}

    def suspend(self):
        """A disconnected extension can reattach; it is not an explicit Stop."""
        with self.lock:
            if self.session:
                self.session.stop.set()

    @staticmethod
    def _target(s):
        return PlaylistTarget(s.client, s.playlist, s.local_id, s.title, s.episode_id, s.stop)

    def _apply_mutation_result(self, s, result: MutationResult):
        with self.lock:
            s.accept_mutation(result, self.ctx.now_fn())

    def _mirror(self, s, tracks):
        """Atomically publish confirmed contents, retaining an unchanged prefix."""
        self.playlists.publish(s.local_id, s.title, s.tracks, tracks, self.ctx.now_fn())

    def _remote_tracks(self, s, *, expected=None):
        return self.playlists.read(s.client, s.playlist, expected=expected)

    def seen(self, menu_id):
        with self.operation:
            result, keys = self.listening.seen(self.session, menu_id)
            worker = getattr(self.ctx, "enrich_worker", None)
            if worker and keys:
                worker.prioritize(list(keys))
            return result

    def _refresh_candidates(self, s):
        """Publish a prepared upgrade only into the still-current session."""
        if s is not self.session:
            return
        upgrade = self.preparation.upgrade(s, RadioCandidates, getattr(self.ctx, 'rec_worker', None))
        if upgrade is None:
            return
        with self.lock:
            if s is not self.session or s.stop.is_set():
                return
            s.candidates, s.directions = upgrade.candidates, upgrade.directions
            s.exhausted = False
            s.feedback_dirty = False
            self._persist(s)
        logger.info("Radio upgraded fallback candidates: regions=%d", len(upgrade.candidates.modes))

    def _refresh_now(self, s):
        """Schedule one bounded-cadence sample even when no Radio page is polling."""
        now = self.ctx.now_fn()
        with self.lock:
            candidates = s.candidates
            if (s is not self.session or s.stop.is_set() or not self.work.accepting
                    or not candidates.modes or s.now_refreshing
                    or now - candidates.now_sampled_at < radio_tuning.NOW_REFRESH_SECONDS
                    or (s.now_requested_at is not None
                        and now - s.now_requested_at < radio_tuning.NOW_REFRESH_SECONDS)):
                return
            s.now_refreshing, s.now_requested_at = True, now

        def refresh():
            try:
                with self.preparation.sample_now(s, candidates, now) as (snapshot, sample):
                    with self.operation, self.lock:
                        if s is self.session and s.candidates is candidates and not s.stop.is_set():
                            snapshot.publish(lambda: candidates.apply_now(sample))
            except Exception:
                logger.debug("Radio current-listening refresh deferred", exc_info=True)
            finally:
                with self.lock:
                    s.now_refreshing = False
        try:
            self.work.start_thread(refresh, name="radio-now")
        except Exception:
            with self.lock:
                s.now_refreshing = False
            raise

    def _reject_tail(self, s, tail, track):
        self.listening.reject(s, track)
        tail.clear_wait()
        tail.proposed = self.policy.replenish(s.candidate_view(), s.history_view(), tail.proposed, tail.slot)
        self._persist(s)

    def _apply_tail(self, s, tail):
        """Coordinate policy/playback between confirmed transactions, bounded for Stop."""
        tail.saved = False
        for _ in range(7):
            if s.stop.is_set():
                return False
            result = self.mutations.read_tail(self._target(s), tail, s.playlist_view(self.ctx.now_fn()))
            self._apply_mutation_result(s, result)
            self._persist(s)
            if result.rejected:
                self._reject_tail(s, tail, result.rejected)
                continue
            if result.tracks is None:
                return False
            remote = list(result.remote)
            ids = tuple(row['videoId'] for row in remote)
            self.listening.observe(s)
            player = self.listening.player(s)
            if player and player.get("video_id") in ids:
                self.listening.record(s, ids.index(player["video_id"]))
            # An acknowledged write can have raced a song change. Replan the
            # unsent future only after pending readback has settled that write.
            view, history = s.candidate_view(), s.history_view()
            if any(self.policy.genre_mismatch(view, history, t) for t in tail.proposed):
                valid = [t for t in tail.proposed if not self.policy.invalid(view, history, t)]
                tail.proposed = self.policy.replenish(view, history, valid, tail.slot)
                tail.choice_id = None
                self._persist(s)
            decision = tail.plan(remote, s.playlist_view(self.ctx.now_fn()), player)
            if decision.complete:
                return True
            edit = decision.edit
            if edit is None or s.stop.is_set():
                return False
            result = self.mutations.edit_tail(self._target(s), s.edits, s.playlist_view(self.ctx.now_fn()), edit, tuple(remote))
            self._apply_mutation_result(s, result)
            self._persist(s)
            if result.status == 'rejected':
                if result.rejected:
                    self._reject_tail(s, tail, result.rejected)
                    continue
                return False
            if result.tracks is None:
                return False
            s.refresh_needed = True
            self._persist(s)
        return False

    def _finish_replacement(self, s):
        replacement = s.replacement
        if not self._apply_tail(s, replacement):
            return False
        with self.lock:
            # Finish an in-flight saved edit before applying the latest choice,
            # without waiting for playback to adopt a superseded direction.
            if s.requested_direction:
                s.edits.finish(replacement)
                return True
        replacement.saved = True
        s.refresh_needed = True
        self.listening.observe(s)
        if not s.browser.get("playback_synced"):
            return False
        with self.lock:
            branch = s.directions.slots[replacement.slot]
            branch["tracks"] = replacement.proposed
            s.directions.active = replacement.slot
            s.directions.revision += 1
            s.directions.menu_id = uuid4().hex
            s.edits.finish(replacement)
            s.exhausted = False
            s.error = None
            saved = {t["video_id"] for t in s.tracks}
            s.choices = {video: choice for video, choice in s.choices.items() if video in saved}
        self.listening.update(s)
        return True

    def select(self, proposal_id, menu_id):
        # Accept the user's choice without waiting on the browser or YouTube.
        # The observer owns playlist writes and finishes any in-flight append first.
        with self.lock:
            s = self.session
            result, changed = self.listening.select(s, proposal_id, menu_id)
            if changed:
                self._persist(s)
            return {**result, **self.status()} if result['ok'] else result

    @work_span("radio: apply stream tuning", announce=True)
    def tune(self, values):
        values = radio_tuning.controls(values)
        with self.operation:
            s = self.session
            if not self._is_current(s) or not s.directions:
                return {"ok": False, **self.status()}
            if values == s.candidates.tuning:
                return {"ok": True, **self.status()}
            # Reuse the prepared pools with the new source mix. This route runs
            # off the playback poll and never changes durable taste or play evidence.
            candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn(), values,
                                         now_source=s.candidates)
            with self.lock:
                self.listening.tune(s, candidates)
                self._persist(s)
            return {"ok": True, **self.status()}

    def start(self, seed_video_id=None, local_date=None, menu_id=None, *, playlist_id=None):
        self.join()
        with self.operation:
            day = date.fromisoformat(local_date) if local_date else datetime.fromtimestamp(self.ctx.now_fn()).date()
            with self.lock:
                if playlist_id is None and self._is_current(self.session):
                    return {"ok": True, "joined": True, **self.status()}
                try:
                    identity, client = client_for(self.ctx.store, self.ctx.client_provider())
                except ValueError as exc:
                    return {"ok": False, "reason": str(exc)}
                if not self.ctx.bridge.connected:
                    return {"ok": False, "reason": "Connect the YouTube Music extension first."}
            try:
                offered = choice_evidence.opportunity(self.ctx.store, menu_id or self.offer_id) if (menu_id or self.offer_id) else None
                assignment = {'comparison': offered.get('comparison')} if offered else {}
                s = self.launch.resolve(RadioCandidates(self.ctx.store, self.ctx.now_fn(), **assignment), client, identity, day,
                    seed_video_id=seed_video_id, playlist_id=playlist_id, menu_id=menu_id or self.offer_id)
            except ValueError as exc:
                return {"ok": False, "reason": str(exc)}
            if s is None:
                self.offer = None
                return {"ok": False, "refresh_seeds": True, **self.status()}
            with self.lock:
                if self.session:
                    self.session.stop.set()
                self.session = s
            try:
                if not self.launch.create(s, from_playlist=playlist_id is not None):
                    return {"ok": False, "reason": "Stopped during start."}
                with self.lock:
                    self._persist(s)
                    if s.stop.is_set():
                        return {"ok": False, "reason": "Stopped during start."}
                try:
                    self._complete_start(s)
                except PlaylistPending:
                    pass
                if not s.stop.is_set():
                    self._start_monitor(s)
            except Exception as exc:
                logger.warning("Radio Session start failed", exc_info=True)
                with self.lock:
                    s.error = str(exc)
                    if not s.stopped_by_user:
                        s.move(Phase.CONFLICT)
                    s.stop.set()
                    self._persist(s)
                return {"ok": False, "reason": str(exc), **self.status()}
            finally:
                with self.lock:
                    s.settle()
            return {"ok": True, **self.status()}

    def _complete_start(self, s):
        prepared = self.launch.confirm(s)
        with self.lock:
            self.launch.accept(s, prepared)
            self.listening.update(s)
            self._persist(s)
            if not s.stop.is_set():
                self.playback.navigate(s.playlist, prepared.tracks[0]['video_id'])

    def _recover(self, s):
        """Reconcile our in-flight write without discarding unrelated saved edits."""
        result = self.mutations.recover(self._target(s), s.edits, s.playlist_view(self.ctx.now_fn()))
        self._apply_mutation_result(s, result)
        self._persist(s)
        if result.status != 'confirmed':
            return False
        s.move(s.running_phase())
        s.error = None
        return True

    def _wait_for_skip(self, s, video_id, deadline):
        while time.monotonic() < deadline and not s.stop.wait(.25):
            try:
                self.listening.observe(s)
                current = (s.browser.get("player") or {}).get("video_id")
                if current and current != video_id:
                    return True
            except PlaylistPending:
                pass
        return False

    def skip(self, video_id):
        """Repair radio's saved future, then advance exactly the displayed song.

        A disabled/stale native Next control falls back to the confirmed saved
        successor. Selecting that exact successor cannot skip a second song
        when a native click's acknowledgement is lost.
        """
        playing = self.ctx.bridge.now_playing or {}
        if not self.session or self.session.stop.is_set():
            self.join()
        s = self.session
        if not s or s.stopped_by_user or playing.get("playlist_ytm_id") != s.playlist:
            return None
        deadline = time.monotonic() + 45
        with self.operation:
            s.skip_requested = True
            try:
                while time.monotonic() < deadline and not s.stop.is_set():
                    try:
                        self.listening.observe(s)
                        player = self.listening.player(s)
                        if player is None:
                            current = s.browser.get("player") or {}
                            if current.get("playlist_ytm_id") and current["playlist_ytm_id"] != s.playlist:
                                return {"ok": True}  # User navigated away while this request waited.
                            s.stop.wait(.25)
                            continue
                        if not player.get("video_id"):
                            s.stop.wait(.25)
                            continue
                        if player["video_id"] != video_id:
                            return {"ok": True}
                        if s.halted and not self._recover(s):
                            # An unrelated edit can still contain a usable next
                            # song. Read it without removing or overwriting it.
                            ids = [t["videoId"] for t in self._remote_tracks(s)]
                        else:
                            self._advance(s)
                            self.listening.observe(s)
                            current = (s.browser.get("player") or {}).get("video_id")
                            if current and current != video_id:
                                return {"ok": True}
                            ids = [t["video_id"] for t in s.tracks]
                            if s.replacement or s.requested_direction or s.pending:
                                s.stop.wait(.25)
                                continue
                        index = ids.index(video_id) if video_id in ids else -1
                        if index >= 0 and index + 1 < len(ids):
                            successor = ids[index + 1]
                            status, result = 0, {"error": "Native queue is still refreshing"}
                            if s.browser.get("playback_synced"):
                                status, result = self.playback.next(video_id)
                            if (status == 200 and result.get("ok")
                                    and self._wait_for_skip(s, video_id, min(deadline, time.monotonic() + 3))):
                                return {"ok": True}
                            # Refresh/navigation can race the native button. Get
                            # a fresh identity before using the saved successor.
                            self.listening.observe(s)
                            current = (s.browser.get("player") or {}).get("video_id")
                            if current and current != video_id:
                                return {"ok": True}
                            logger.info("Radio skip selecting saved successor: playlist=%s from=%s to=%s reason=%s",
                                        s.playlist, video_id, successor, result.get("error"))
                            self.playback.navigate(s.playlist, successor)
                            # Navigation is sent once; subsequent observations
                            # confirm it rather than repeating a skip command.
                            if self._wait_for_skip(s, video_id, deadline):
                                return {"ok": True}
                            break
                        if s.exhausted:
                            return {"ok": False, "error": "No more playable songs are available in this radio session."}
                    except PlaylistPending:
                        pass
                    except PlaylistChanged as exc:
                        if not s.stopped_by_user:
                            s.move(Phase.CONFLICT)
                        s.error = str(exc)
                    s.stop.wait(.25)
                return {"ok": False, "error": "Radio is still preparing its next playable song. Try skip again shortly."}
            finally:
                s.skip_requested = False
                self._persist(s)

    def _monitor(self, s):
        try:
            while not s.stop.wait(2):
                if not self.work.accepting or not self.ctx.bridge.connected:
                    break
                try:
                    self._refresh_now(s)
                    with self.operation:
                        if s is not self.session or s.stop.is_set():
                            break
                        if s.initial_tracks:
                            self._complete_start(s)
                        else:
                            self.listening.observe(s)
                            if s.halted:
                                # A conflict pauses mutations, not the radio UI.
                                # Resume only after readback returns to a state
                                # owned by this transaction; never overwrite an edit.
                                if not self._recover(s):
                                    continue
                            self.listening.refresh_metadata(s)
                            self._refresh_candidates(s)
                            self._advance(s)
                        self._persist(s)
                except PlaylistPending:
                    with self.lock:
                        if not s.halted:
                            s.error = None
                        self._persist(s)
                except PlaylistChanged as exc:
                    logger.warning("Radio saved-playlist conflict: playlist=%s current=%s reason=%s",
                                   s.playlist, s.observed_video, exc)
                    with self.lock:
                        s.error = str(exc)
                        if not s.stopped_by_user:
                            s.move(Phase.CONFLICT)
                        if not s.playback_confirmed:
                            s.stop.set()
                        self._persist(s)
                except Exception as exc:
                    logger.warning("Radio Session append failed", exc_info=True)
                    with self.lock:
                        s.error = str(exc)
        finally:
            with self.lock:
                s.stop.set()
                s.monitoring = False
                if s.phase == Phase.STOPPING:
                    s.move(Phase.STOPPED)
                self._persist(s)

    def advance(self, s):
        """Observe actual playback; append only to this session's saved playlist.

        Read back every addition. Keep an uncertain append pending so the next
        observation checks whether it landed before considering another write.
        """
        with self.operation:
            try:
                self._advance(s)
            finally:
                self._persist(s)

    def _advance(self, s):
        if s.stop.is_set() or self.listening.player(s) is None:
            return
        # Resolve an uncertain append before a switch or feedback replacement.
        if s.pending is not None:
            if not self._append(s):
                return
        # A resumed transaction may already have added the playing song remotely
        # while its last local mirror failed. Reconcile it before looking up position.
        if s.replacement:
            changed = self.listening.feedback(s)
            if changed and s.replacement.waiting is None:
                replacement = s.replacement
                view, history = s.candidate_view(), s.history_view()
                valid = [t for t in replacement.proposed[:LOOKAHEAD] if not self.policy.invalid(view, history, t)]
                replacement.proposed = self.policy.replenish(view, history, valid, replacement.slot)
            if not self._finish_replacement(s):
                return
        if s.requested_direction:
            with self.lock:
                request = s.requested_direction
                view, history = s.candidate_view(), s.history_view()
                if any(self.policy.genre_mismatch(view, history, t) for t in request["tracks"]):
                    request["tracks"] = self.policy.replenish(view, history, request['tracks'], request['slot'])
                    request["choice"] = None
                s.edits.begin(RadioPlaylistTail(s.tracks, request["tracks"], request["slot"],
                                                choice_id=request["choice"]))
                s.choices.clear()
                if request["choice"]:
                    s.choices[request["tracks"][0]["video_id"]] = request["choice"]
                s.edits.requested = None
                self._persist(s)
            if not self._finish_replacement(s):
                return
        player = self.listening.player(s)
        position = next((i for i, t in enumerate(s.tracks)
                         if player and t["video_id"] == player.get("video_id")), None)
        if position is None:
            return
        self.listening.record(s, position)
        changed = self.listening.feedback(s)
        if position < s.furthest:
            self.listening.update(s, force=changed)
            return
        scheduled = s.tracks[position + 1:]
        view, history = s.candidate_view(), s.history_view()
        invalid = [t for t in scheduled if self.policy.invalid(view, history, t)]
        if invalid:
            # A genre pin is temporary. Those songs become eligible again when
            # the DJ moves the dial or the currently playing genre changes.
            s.rejected.update(t["video_id"] for t in invalid if not self.policy.genre_mismatch(view, history, t))
            s.directions.blocked.update(s.rejected)
            valid = [t for t in scheduled if t not in invalid]
            proposal = self.policy.replenish(s.candidate_view(), history, valid, s.directions.active)
            s.edits.begin(RadioPlaylistTail(s.tracks, proposal, s.directions.active))
            if not self._finish_replacement(s):
                return
        self.listening.update(s, force=changed)
        # A bounded pass keeps Stop responsive even when someone skips rapidly.
        for _ in range(LOOKAHEAD + 1):
            player = self.listening.player(s)
            if s.stop.is_set() or player is None:
                return
            position = next((i for i, t in enumerate(s.tracks)
                             if t["video_id"] == player.get("video_id")), None)
            if position is None:
                return
            self.listening.record(s, position)
            self.listening.update(s)
            if len(s.tracks) - position - 1 >= LOOKAHEAD:
                s.error = None
                return
            if not self._append(s):
                return

    def _append(self, s):
        if s.pending is None:
            track = self.policy.pick(s.candidate_view())
            if track is None:
                s.exhausted = True
                return False
            s.selected.append(track)
            s.edits.begin(AppendOperation(track))
        s.settle()
        self._persist(s)
        operation = s.edits.append
        result = self.mutations.append(self._target(s), s.edits, s.playlist_view(self.ctx.now_fn()))
        self._apply_mutation_result(s, result)
        if result.status == 'confirmed':
            with self.lock:
                s.edits.finish(operation)
                s.exhausted = False
                s.error = None
                s.refresh_needed = True
            self._persist(s)
            self.listening.observe(s)
            return True
        if result.status in {'rejected', 'discarded'}:
            if result.rejected:
                self.listening.reject(s, result.rejected)
            s.edits.finish(operation)
            s.selected = list(s.tracks)
            s.error = None
            self._persist(s)
            return True
        return False
