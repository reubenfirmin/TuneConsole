"""Saved-playlist radio, observed through the extension's native playback state.

Start with three songs in a new YouTube playlist; then keep one song ahead of
the currently playing song. Ask Music's native refresh handler to fetch the saved
playlist, without supplying playback items or reloading the tab.
"""
import json
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from uuid import uuid4
from weakref import ref
from types import SimpleNamespace

from starlette.websockets import WebSocketDisconnect

from yt_playlist.core.activity import (activity_for, cancellable_work, check_stopping, WorkClosing)
from yt_playlist.core.accounts import client_for
from yt_playlist.core.bridge import BridgeError
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
from yt_playlist.web.radio_persistence import RadioPersistence, encode
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
        self.tuning_serial = 0
        self.tuning_owner = None
        self.tuning_pending = None
        self.tuning_unconfirmed = None
        self.session = None
        self.offer = None
        self.offer_id = None
        paused = lambda: getattr(ctx.store, 'activity_paused', False)
        self.persistence = RadioPersistence(ctx.store.radio_checkpoints, paused=paused, snapshot=self._checkpoint)
        self.playback = RadioPlayback(ctx.bridge)
        self.playlists = RadioPlaylistIO(ctx.store.playlists, paused=paused)
        self.mutations = RadioMutations(self.playlists, self.persistence, ctx.store.tracks,
                                       lambda: self.ctx.now_fn(), paused=paused,
                                       rotation_facts=lambda keys: ctx.store.radio_catalog.rotation_facts(keys))
        self.policy = RadioPolicy(ctx.store, lambda: self.ctx.now_fn())
        self.listening = RadioListening(ctx.store, self.playback, self.policy, self.lock, lambda: self.ctx.now_fn())
        self.launch = RadioLaunch(ctx.store, self.playlists, self.persistence, self.policy,
                                  self.lock, lambda: self.ctx.now_fn())
        self.preparation = RadioPreparation(ctx.store, self.policy, self.work, lambda: self.ctx.now_fn())
        self.preparing = False
        self.checked_playlist = None
        self.launch_requests = {}
        self.cancelled_launches = OrderedDict()

    def _playing(self):
        """The extension's fresh playback presence is authoritative, not saved state."""
        return self.playback.current()

    def _is_current(self, s):
        return self.listening.current(s)

    def prepare(self):
        """Schedule preparation without waiting on session work or browser replies."""
        maintenance = getattr(self.ctx, 'storage_maintenance', None)
        if self.ctx.store.activity_paused or (maintenance is not None and not maintenance.ready.is_set()):
            return
        # Playback heartbeats call this from the WebSocket response pump. A busy
        # session can retry on the next heartbeat; it must not hold up its own replies.
        if not self.lock.acquire(blocking=False):
            return
        try:
            playing = self._playing().get("playlist_ytm_id")
            settled = (self.session and (self.session.monitoring or self.session.stopped_by_user
                        or not self._is_current(self.session))) or (
                        self.session is None and playing == self.checked_playlist)
            if (self.preparing or not self.work.accepting or (self.offer is not None and settled)
                    or self.launch_requests):
                return
            self.preparing = True
            serial = self.tuning_serial
        finally:
            self.lock.release()
        def warm():
            try:
                if self.ctx.store.activity_paused or (maintenance is not None and not maintenance.ready.is_set()):
                    return
                with self.ctx.store.priority.background(check_stopping), \
                        cancellable_work(lambda: serial != self.tuning_serial):
                    check_stopping()
                    self.join()
                    self.checked_playlist = playing
                    if self.offer is None and self.session is None:
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

    def state(self, *, reader=None):
        """Project the live or saved session without loading and filtering seed menus."""
        with self.lock:
            if self.session is not None or self.launch_requests:
                return self.status()
        if reader is not None:
            return self.launch.checkpoint_view(self._playing(), reader=reader)
        with self.ctx.store.read_view() as reader:
            return self.launch.checkpoint_view(self._playing(), reader=reader)

    def bootstrap(self, *, reader=None):
        """Return the panel's session and currently eligible seed suggestions."""
        state = self.state(reader=reader)
        if state.get('active'):
            return {"state": state, "seeds": [], "seedMenu": None,
                    "playback": {"connected": self.ctx.bridge.connected, "now_playing": self._playing() or None}}
        store = self.ctx.store if reader is None else reader
        try:
            offer = json.loads(store.settings.get_setting("radio_seed_offer") or "{}")
        except (ValueError, TypeError):
            offer = {}
        tracks = self.offer if self.offer is not None else offer.get("tracks", [])
        return {"state": state, "seeds": self._eligible_seeds(tracks, store=store),
                "seedMenu": self.offer_id or offer.get("menu_id"),
                "playback": {"connected": self.ctx.bridge.connected, "now_playing": self._playing() or None}}

    def _persist(self, s):
        """Keep a resumable pointer using the existing settings API."""
        self.persistence.save(s)

    def _checkpoint(self, s):
        """Capture current state under save ownership; never wait for I/O here."""
        with self.lock:
            if s is not self.session or not s.playlist:
                return
            s.settle()
            return encode(s)

    def join(self, *, timeout=-1, cancelled=None):
        """Resume only owned playback; never navigate from a restored checkpoint."""
        with self.lock:
            s = self.session
            if s and (s.monitoring or s.stopped_by_user or not self.ctx.bridge.connected):
                return self.status()
        if cancelled is None:
            if not self.operation.acquire(timeout=timeout):
                return self.status()
        else:
            while not self.operation.acquire(timeout=.05):
                if cancelled.is_set():
                    return self.status()
        try:
            if self.session is None:
                playing, clients = self._playing(), self.ctx.client_provider() or {}
                try:
                    state = self.persistence.load()
                    if state and state.get("stopped") and state.get("playlist") == playing.get("playlist_ytm_id"):
                        return self.status()
                    s, _ = self.launch.resume(playing, clients, RadioCandidates)
                    with self.lock:
                        self._expire_cancelled_launches()
                        if s and s.launch_id in self.cancelled_launches:
                            s.request_stop()
                            s.move(Phase.STOPPED)
                        self.session = s
                    if s and s.stopped_by_user:
                        self._persist(s)
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
        finally:
            self.operation.release()
        return self.status()

    def _start_monitor(self, s):
        s.monitoring = True
        try:
            self.work.start_thread(self._monitor, args=(s,), name="radio-session")
        except Exception:
            s.monitoring = False
            raise

    def _eligible_seeds(self, tracks, *, store=None):
        """Recheck cached suggestions without rebuilding models or changing the offer."""
        from yt_playlist.rec.radio_candidates import eligible_seeds
        store = self.ctx.store if store is None else store
        return eligible_seeds(store, tracks, self.ctx.now_fn())

    def seeds(self):
        with self.operation:
            if not self._eligible_seeds(self.offer):
                candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn())
                self.offer, self.offer_id = self.listening.seed_offer(candidates)
            return {"tracks": self._eligible_seeds(self.offer), "menu_id": self.offer_id}

    @property
    def active(self):
        with self.lock:
            return bool(self._is_current(self.session))

    def status(self):
        with self.lock:
            s = self.session
            state = status_view(s, active=bool(self._is_current(s)),
                                connected=self.ctx.bridge.connected, now=time.monotonic())
            if s and s.stopped_by_user:
                state['starting'] = False
            state['starting'] |= any(not request.is_set() for request in self.launch_requests)
            state['stopping'] = any(request.is_set() for request in self.launch_requests)
            state['stopping_launches'] = list(dict.fromkeys(
                owner for request, owner in self.launch_requests.items()
                if owner is not None and request.is_set()))
            pending = self.tuning_pending
            if pending and pending[0] == self.tuning_serial and not state['stopped']:
                state['tuning_pending'] = dict(pending[1])
            state['tuning_confirmed'] = self.tuning_unconfirmed is not s or s is None
            return state

    def history_tracks(self, state, *, reader=None):
        """Render live metadata even when repair deliberately discarded catalog writes."""
        history = state.get('history', [])
        store = self.ctx.store if reader is None else reader
        stored = {t['video_id']: t for t in store.charts.video_tracks_detail(
            [t['video_id'] for t in history])}
        return [{**t, **stored.get(t['video_id'], {})} for t in history]

    def retain_provenance(self):
        with self.operation:
            if self.session and self.session.candidates.prepared is not None:
                self.session.candidates.prepared.retain(self.ctx.now_fn())
            self.launch.retain_provenance()

    def stop(self, *, launch_id=None):
        with self.lock:
            if launch_id is not None:
                # A Cancel can overtake its Start on the network. Keep a bounded
                # receipt beyond the browser's 45-second launch deadline so that
                # delayed admission cannot create a session after cancellation.
                self._expire_cancelled_launches()
                self.cancelled_launches[launch_id] = time.monotonic()
                self.cancelled_launches.move_to_end(launch_id)
                while len(self.cancelled_launches) > 256:
                    self.cancelled_launches.popitem(last=False)
            pending = bool(self.launch_requests)
            for request, owner in self.launch_requests.items():
                if launch_id is None or owner == launch_id:
                    request.set()
            if launch_id is None:
                self.tuning_serial += 1
        if self.session is None and not pending:
            self.join(timeout=0)
        with self.lock:
            s = self.session
            if s and (launch_id is None or s.launch_id == launch_id):
                s.request_stop()
                if not s.monitoring:
                    s.move(Phase.STOPPED)
            else:
                s = None
        if s:
            self._persist(s)
        else:
            self.persistence.stop_saved(launch_id)
        return {"ok": True, **self.status()}

    def suspend(self):
        """A disconnected extension can reattach; it is not an explicit Stop."""
        with self.lock:
            if self.session:
                self.session.stop.set()
                self.session.wake.set()

    def resume_playback(self, playlist, video, episode, *, intent):
        """An explicit Play may reclaim the saved session; reconnect alone never navigates."""
        with self.persistence.lock:
            state = self.state()
            with self.lock:
                if self.session is not None:
                    state = self.status()
                target = state.get('resume_player')
                if (not self.ctx.bridge.connected or not target or
                        (target['playlist_ytm_id'], target['video_id'], target['radio_session_id'])
                        != (playlist, video, episode)):
                    return {'ok': False}
                try:
                    sent = self.playback.navigate(playlist, video, intent=intent)
                except BridgeError:
                    logger.debug('Radio resume deferred', exc_info=True)
                    return {'ok': False}
        # Scheduling a navigation is not confirmation that playback has started.
        return {'ok': bool(sent)}

    @staticmethod
    def _target(s):
        return PlaylistTarget(s.client, s.playlist, s.local_id, s.title, s.episode_id, s.stop, s.identity)

    def _apply_mutation_result(self, s, result: MutationResult):
        with self.lock:
            s.accept_mutation(result, self.ctx.now_fn())

    def _mirror(self, s, tracks):
        """Atomically publish confirmed contents, retaining an unchanged prefix."""
        self.playlists.publish(s.local_id, s.title, s.tracks, tracks, self.ctx.now_fn())

    def _remote_tracks(self, s, *, expected=None):
        return self.playlists.read(s.client, s.playlist, expected=expected)

    def seen(self, menu_id):
        if self.session is None:
            self.join()
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
                    or not getattr(candidates, 'model_ready', True) or not candidates.modes or s.now_refreshing
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
        tail.proposed = self.policy.replenish(s.candidate_view(), s.history_view(), tail.proposed, tail.slot,
                                             excluded=tail.excluded)
        self._persist(s)

    def _apply_tail(self, s, tail):
        """Coordinate policy/playback between confirmed transactions, bounded for Stop."""
        tail.saved = False
        result = None
        for _ in range(7):
            if s.stop.is_set():
                return False
            if result is None:
                result = self.mutations.read_tail(self._target(s), tail, s.playlist_view(self.ctx.now_fn()))
                self._apply_mutation_result(s, result)
                self._persist(s)
            if result.rejected:
                self._reject_tail(s, tail, result.rejected)
                result = None
                continue
            if result.tracks is None:
                return False
            remote = list(result.remote)
            ids = tuple(row['videoId'] for row in remote)
            player = self.listening.player(s)
            playing = self.playback.current()
            # The monitor already observed playback before entering this edit.
            # While confirming an explicit addition, reuse that fresh sample
            # only if the independent heartbeat still agrees. Cleanup, song
            # changes and stale samples always get a new browser observation.
            confirmed_player = (tail.play and not tail.play.get('sent') and not s.requested_direction
                                and player and playing.get('playlist_ytm_id') == s.playlist
                                and playing.get('video_id') == player.get('video_id'))
            if not confirmed_player:
                self.listening.observe(s)
                player = self.listening.player(s)
            position = self.listening.position(s, player.get('video_id')) if player else None
            if position is not None and not player.get('paused'):
                self.listening.record_player(s, position, player)
            # An acknowledged write can have raced a song change. Replan the
            # unsent future only after pending readback has settled that write.
            view, history = s.candidate_view(), s.history_view()
            if any(self.policy.genre_mismatch(view, history, t) for t in tail.proposed):
                valid = [t for t in tail.proposed if not self.policy.invalid(view, history, t)]
                tail.proposed = self.policy.replenish(view, history, valid, tail.slot, excluded=tail.excluded)
                tail.choice_id = None
                self._persist(s)
            decision = tail.plan(remote, s.playlist_view(self.ctx.now_fn()), self.listening.queue_player(s, player))
            # The target is playable as soon as its addition is confirmed. Remove
            # the old future while Music loads it, rather than delaying playback
            # for that independent write and its readback.
            if tail.play and not tail.play.get('sent') and tail.play['video_id'] in ids:
                self._persist(s)
                if self._handoff_song(s, tail):
                    # Selection can advance immediately. Recheck the observed
                    # song before sending the already-planned cleanup edit.
                    continue
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
                    result = None
                    continue
                return False
            if result.tracks is None:
                return False
            s.refresh_needed = True
            self._persist(s)
            # edit_tail already returned a complete, confirmed readback. Plan
            # from that snapshot instead of fetching the same playlist again.
        return False

    def _handoff_song(self, s, replacement):
        with self.lock:
            play = replacement.play
            if (not play or play.get('sent') or s.requested_direction or s.stop.is_set()
                    or s is not self.session or not self._is_current(s)):
                return
            video = play['video_id']
            player = self.listening.player(s, allow_paused=True)
            if (not player or replacement.play_prefix is None or video in s.directions.blocked
                    or video not in {t['video_id'] for t in replacement.proposed}
                    or video not in {t['video_id'] for t in s.tracks}):
                return
            # An uncertain delivery is never repeated. This live flag, like the
            # playback intent itself, is deliberately absent from checkpoints.
            play['sent'] = True
            if player.get('video_id') == video and not player.get('paused'):
                return
            try:
                sent = self.playback.navigate(s.playlist, video, intent=play['intent'])
                if sent:
                    logger.info("Radio song handoff: playlist=%s video=%s elapsed=%.3fs",
                                s.playlist, video,
                                time.monotonic() - play.get('requested_at', time.monotonic()))
                return sent
            except BridgeError:
                logger.warning("Radio playback handoff unconfirmed: playlist=%s video=%s",
                               s.playlist, video, exc_info=True)
                s.selection_error = 'Playback not confirmed. Check the current song before choosing again.'

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
        if not replacement.play:
            self.listening.observe(s)
            if not s.browser.get("playback_synced"):
                return False
        with self.lock:
            # A newer request may arrive while the browser acknowledgement waits.
            if s.requested_direction:
                s.edits.finish(replacement)
                return True
            if s.stop.is_set() or not self._is_current(s):
                return False
        if not self._replan_selection(s, replacement=replacement):
            return False
        self.listening.update(s)
        return True

    def _replan_selection(self, s, *, request=None, replacement=None):
        """Plan a queued or confirmed selection without owning the session lock."""
        with self.lock:
            if (s is not self.session or s.stop.is_set() or not self._is_current(s)
                    or s.requested_direction is not request or s.replacement is not replacement):
                return False
            view, history = s.candidate_view(), s.history_view()
            source, dirty = s.directions, s.feedback_dirty
            before = source.snapshot()
            metadata_revision = s.metadata_revision
            if request is not None:
                selection = deepcopy(request)
            else:
                selection = {'tracks': deepcopy(replacement.proposed), 'slot': replacement.slot,
                             'excluded': list(replacement.excluded), 'play': deepcopy(replacement.play)}
        # Both genre validation and continuation can read the catalog or load
        # the restored candidate model. Work only against the detached menu.
        planning = replace(view, directions=before)
        mismatch = request is not None and any(
            self.policy.genre_mismatch(planning, history, track) for track in selection['tracks'])
        tracks = selection['tracks']
        if mismatch or (selection.get('play') and replacement is not None):
            tracks = self.policy.replenish(planning, history, tracks, selection['slot'],
                                           excluded=selection.get('excluded', ()))
        with self.lock:
            if (s is not self.session or s.stop.is_set() or not self._is_current(s)
                    or s.candidate_view() != view or s.history_view() != history
                    or s.requested_direction is not request or s.replacement is not replacement
                    or s.feedback_dirty != dirty or s.metadata_revision != metadata_revision
                    or vars(source) != vars(before)):
                return False
            if request is not None:
                choice = None if mismatch else selection['choice']
                s.edits.begin(RadioPlaylistTail(s.tracks, tracks, selection['slot'], choice_id=choice,
                    play=selection.get('play'), excluded=selection.get('excluded', ())))
                s.choices.clear()
                if choice and tracks:
                    s.choices[tracks[0]['video_id']] = choice
                s.edits.requested = None
            else:
                branch = source.slots[replacement.slot]
                branch['tracks'] = tracks
                source.active = replacement.slot
                source.revision += 1
                source.menu_id = uuid4().hex
                s.edits.finish(replacement)
                s.exhausted = False
                s.error = None
                saved = {track['video_id'] for track in s.tracks}
                s.choices = {video: choice for video, choice in s.choices.items() if video in saved}
                if replacement.play and replacement.play['video_id'] not in saved:
                    s.selection_error = 'That song could not be added. Choose another song from the stream.'
        self._persist(s)
        return True

    def select(self, proposal_id, menu_id, video_id=None, *, intent=None):
        # Accept the user's choice without waiting on the browser or YouTube.
        # The observer owns playlist writes and finishes any in-flight append first.
        requested_at = time.monotonic()
        if self.session is None:
            self.join()
        with self.lock:
            s = self.session
            result, changed = self.listening.select(s, proposal_id, menu_id, video_id, intent=intent)
            if changed:
                # A newer explicit choice owns the future even after its
                # request/replacement journal has been acknowledged and cleared.
                self.tuning_serial += 1
                if video_id is not None and intent is None:
                    s.edits.requested['play']['intent'] = self.ctx.bridge.begin_playback_intent()
                if video_id is not None:
                    s.edits.requested['play']['requested_at'] = requested_at
                s.wake.set()
        if changed:
            self._persist(s)
        return {**result, **self.status()} if result['ok'] else result

    def reserve_tuning(self):
        """Own Apply before its access check or catalog reads can wait."""
        with self.lock:
            self.tuning_serial += 1
            s = self.session
            self.tuning_owner = (ref(s) if s else None, s.requested_direction if s else None,
                                 s.replacement if s else None)
            return self.tuning_serial

    @work_span("radio: apply stream tuning", announce=True)
    def tune(self, values, *, serial=None):
        values = radio_tuning.controls(values)
        serial = self.reserve_tuning() if serial is None else serial
        with self.lock:
            if serial != self.tuning_serial:
                return {"ok": False, "tune_reason": "superseded", **self.status()}
            self.tuning_pending = (serial, values)
        # Browser requests remain bounded; their worker waits cooperatively for
        # playlist ownership. A newer explicit action cancels this admission.
        try:
            if self.session is None:
                self.join(cancelled=SimpleNamespace(is_set=lambda: serial != self.tuning_serial or not self.work.accepting))
            while not self.operation.acquire(timeout=.05):
                if serial != self.tuning_serial or not self.work.accepting:
                    with self.lock:
                        if self.tuning_pending and self.tuning_pending[0] == serial:
                            self.tuning_pending = None
                    return {"ok": False, "tune_reason": "superseded", **self.status()}
        except BaseException:
            with self.lock:
                if self.tuning_pending and self.tuning_pending[0] == serial:
                    self.tuning_pending = None
            raise
        try:
            with self.lock:
                s = self.session
                owner = self.tuning_owner
                restored = owner is not None and owner[0] is None
                requested, replacement = owner[1:] if owner else (None, None)
                if restored and s:
                    requested, replacement = s.requested_direction, s.replacement
                current = lambda: (s is self.session and serial == self.tuning_serial and s is not None
                                   and owner is not None and (restored or owner[0]() is s)
                                   and s.requested_direction is requested and s.replacement is replacement)
                if not current() or not self._is_current(s) or s.stop.is_set() or not s.directions:
                    logger.info("Radio tuning refused: reason=inactive_or_superseded serial=%d", serial)
                    return {"ok": False, "tune_reason": "superseded", **self.status()}
                unchanged = values == s.candidates.tuning
                previous_unconfirmed = self.tuning_unconfirmed
                self.tuning_unconfirmed = s
            # Reuse the prepared pools with the new source mix. This route runs
            # off the playback poll and never changes durable taste or play evidence.
            candidates = None
            applied = False
            try:
                if unchanged:
                    applied = True
                else:
                    with cancellable_work(lambda: not current() or s.stop.is_set()), \
                            self.ctx.store.priority.background(check_stopping):
                        candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn(), values,
                                                     now_source=s.candidates)
                    applied = self.listening.tune(s, candidates, current=current)
                if applied:
                    self._persist(s)
                    with self.lock:
                        self.tuning_unconfirmed = None
                        if self.tuning_pending and self.tuning_pending[0] == serial:
                            self.tuning_pending = None
                    s.wake.set()
                logger.info("Radio tuning result: serial=%d accepted=%s values=%s menu=%s",
                            serial, applied, values, s.directions.menu_id)
                return {"ok": applied, **({} if applied else {"tune_reason": "superseded"}), **self.status()}
            except WorkClosing:
                logger.info("Radio tuning superseded: serial=%d", serial)
                return {"ok": False, "tune_reason": "superseded", **self.status()}
            except Exception:
                logger.warning("Radio tuning failed: serial=%d values=%s", serial, values, exc_info=True)
                raise
            finally:
                with self.lock:
                    if not applied and (candidates is None or s.candidates is not candidates):
                        self.tuning_unconfirmed = previous_unconfirmed
                if not applied and candidates is not None and candidates.prepared is not None:
                    candidates.prepared.close()
        finally:
            with self.lock:
                if self.tuning_pending and self.tuning_pending[0] == serial:
                    self.tuning_pending = None
            self.operation.release()

    def _expire_cancelled_launches(self):
        cutoff = time.monotonic() - 120
        while self.cancelled_launches and next(iter(self.cancelled_launches.values())) < cutoff:
            self.cancelled_launches.popitem(last=False)

    @contextmanager
    def launch_request(self, launch_id=None):
        """Reserve cancellation before HTTP preflight or worker admission."""
        with self.lock:
            # Explicit Start supersedes Apply even before a session is loaded.
            # Passive join/restore leaves the reservation available to that session.
            cancelled = threading.Event()
            self._expire_cancelled_launches()
            if launch_id in self.cancelled_launches:
                cancelled.set()
            else:
                self.tuning_serial += 1
            self.launch_requests[cancelled] = launch_id
        try:
            yield cancelled
        finally:
            with self.lock:
                self.launch_requests.pop(cancelled, None)

    def start(self, seed_video_id=None, local_date=None, menu_id=None, *, playlist_id=None,
              intent=None, cancelled=None):
        with (nullcontext(cancelled) if cancelled is not None else self.launch_request()) as cancelled:
            if intent is None:
                intent = self.ctx.bridge.begin_playback_intent()
            try:
                return self._start(seed_video_id, local_date, menu_id, playlist_id=playlist_id,
                                   intent=intent, cancelled=cancelled)
            except WorkClosing:
                cancelled.set()
                return {'ok': False, 'reason': 'Stopped during start.', **self.status()}

    @contextmanager
    def _launch_operation(self, cancelled):
        while not self.operation.acquire(timeout=.05):
            if cancelled.is_set():
                raise WorkClosing('Radio launch stopped')
        try:
            if cancelled.is_set():
                raise WorkClosing('Radio launch stopped')
            yield
        finally:
            self.operation.release()

    def _start(self, seed_video_id, local_date, menu_id, *, playlist_id, intent, cancelled):
        with cancellable_work(cancelled.is_set):
            self.join(cancelled=cancelled)
        with self._launch_operation(cancelled):
            day = date.fromisoformat(local_date) if local_date else datetime.fromtimestamp(self.ctx.now_fn()).date()
            with self.lock:
                if playlist_id is None and self._is_current(self.session):
                    s = self.session
                    # A joined request may have superseded the first request's
                    # intent before its handoff. Adopt that intent only while
                    # delivery is still known not to have happened. Navigation
                    # remains subject to any newer independent Play.
                    if not s.launch_sent and not s.playback_confirmed:
                        s.playback_intent = max(s.playback_intent or 0, intent)
                        self._handoff_start(s)
                    return {"ok": True, "joined": True, **self.status()}
                try:
                    identity, client = client_for(self.ctx.store, self.ctx.client_provider())
                except ValueError as exc:
                    return {"ok": False, "reason": str(exc)}
                if not self.ctx.bridge.connected:
                    return {"ok": False, "reason": "Connect the YouTube Music extension first."}
            try:
                candidates = None
                try:
                    # Cancellation applies to disposable reads and planning.
                    # Once creation starts, uncertain writes still drain through
                    # their existing confirmation and checkpoint protocol.
                    with cancellable_work(cancelled.is_set):
                        offered = choice_evidence.opportunity(self.ctx.store, menu_id or self.offer_id) if (menu_id or self.offer_id) else None
                        assignment = {'comparison': offered.get('comparison')} if offered else {}
                        candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn(), **assignment)
                        s = self.launch.resolve(candidates, client, identity, day,
                            seed_video_id=seed_video_id, playlist_id=playlist_id, menu_id=menu_id or self.offer_id)
                except BaseException:
                    if candidates is not None:
                        candidates.close()
                    raise
            except ValueError as exc:
                return {"ok": False, "reason": str(exc)}
            if s is None:
                candidates.close()
                self.offer = None
                return {"ok": False, "refresh_seeds": True, **self.status()}
            with self.lock:
                if cancelled.is_set():
                    candidates.close()
                    return {'ok': False, 'reason': 'Stopped during start.'}
                if self.session:
                    self.session.stop.set()
                s.stop = cancelled
                s.launch_id = self.launch_requests.get(cancelled)
                s.playback_intent = intent
                self.session = s
            try:
                if not self.launch.create(s, from_playlist=playlist_id is not None):
                    return {"ok": False, "reason": "Stopped during start."}
                self._persist(s)
                with self.lock:
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
        self._persist(s)
        with self.lock:
            self._handoff_start(s)
        # Playback needs the confirmed initial queue, not the four future
        # branches. Build those privately so status and Stop remain available.
        self.listening.initial_directions(s)
        self._persist(s)

    def _handoff_start(self, s):
        """Send the confirmed initial queue once; only a rejected intent may retry.

        Called under the session lock. A queued or uncertain delivery must never
        be repeated by a joining request, preparation retry or restored session.
        """
        if (s is not self.session or s.stop.is_set() or s.launch_sent
                or s.playback_confirmed or s.playback_intent is None
                or s.initial_tracks or not s.tracks):
            return
        s.launch_sent = True
        sent = self.playback.navigate(s.playlist, s.tracks[0]['video_id'], intent=s.playback_intent)
        if sent is False:
            # Bridge rejection establishes that no frame was queued. An error
            # or an uncertain response does not establish that fact.
            s.launch_sent = False

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

    def skip(self, video_id, *, brand_id=None):
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
            recorded_request = False
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
                        if brand_id is not None and (not player.get('brand_known')
                                or (player.get('brand_id') or '') != brand_id):
                            return {'ok': False, 'error': 'Player account is unavailable or different'}
                        if not recorded_request:
                            # This is explicit intent, including immediate/late
                            # skips and the saved-successor fallback. It is not
                            # a dislike or an inferred early-exit judgment.
                            from yt_playlist.core import playback_feedback
                            playback_feedback.record_next(self.ctx.store,
                                s.identity, video_id, player.get('position'),
                                player.get('duration'), s.playlist, self.ctx.now_fn())
                            recorded_request = True
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
                        queued = self.listening.queue_player(s, player).get('video_id')
                        index = ids.index(queued) if queued in ids else -1
                        if index >= 0 and index + 1 < len(ids):
                            successor = ids[index + 1]
                            status, result = 0, {"error": "Native queue is still refreshing"}
                            if s.browser.get("playback_synced"):
                                options = {'brand_id': brand_id} if brand_id is not None else {}
                                status, result = self.playback.next(video_id, **options)
                            if (status == 200 and result.get("ok")
                                    and self._wait_for_skip(s, video_id, min(deadline, time.monotonic() + 3))):
                                return {"ok": True}
                            # Refresh/navigation can race the native button. Get
                            # a fresh identity before using the saved successor.
                            self.listening.observe(s)
                            fresh_player = s.browser.get("player") or {}
                            if brand_id is not None and (not fresh_player.get('brand_known')
                                    or (fresh_player.get('brand_id') or '') != brand_id):
                                return {'ok': False, 'error': 'Player account is unavailable or different'}
                            current = fresh_player.get("video_id")
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
        def song_requested():
            return bool(s.requested_direction and s.requested_direction.get('play')
                        or s.replacement and s.replacement.play)

        try:
            while not s.stop.is_set():
                s.wake.wait(2)
                s.wake.clear()
                if s.stop.is_set() or not self.work.accepting or not self.ctx.bridge.connected:
                    break
                try:
                    if not song_requested():
                        self._refresh_now(s)
                    with self.operation:
                        if s is not self.session or s.stop.is_set():
                            break
                        if s.initial_tracks:
                            self._complete_start(s)
                        else:
                            try:
                                self.listening.observe(s)
                            except WebSocketDisconnect:
                                if (s.stop.is_set() or self.work.closing_reason == 'shutdown'
                                        or not self.ctx.bridge.connected):
                                    break
                                raise
                            if s.halted:
                                # A conflict pauses mutations, not the radio UI.
                                # Resume only after readback returns to a state
                                # owned by this transaction; never overwrite an edit.
                                if not self._recover(s):
                                    continue
                            # A click may have arrived during the browser reply
                            # or metadata work. Recheck before each maintenance
                            # phase rather than using the start-of-pass value.
                            if not song_requested():
                                self.listening.refresh_metadata(s)
                            if not song_requested():
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
                except WorkClosing:
                    break
                except Exception as exc:
                    logger.warning("Radio Session maintenance failed", exc_info=True)
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
                replacement.proposed = self.policy.replenish(view, history, valid, replacement.slot,
                                                             excluded=replacement.excluded)
            if not self._finish_replacement(s):
                return
        if s.requested_direction:
            if not self._replan_selection(s, request=s.requested_direction):
                return
            if not self._finish_replacement(s):
                return
        player = self.listening.player(s)
        position = self.listening.position(s, player.get('video_id')) if player else None
        if position is None:
            return
        self.listening.record_player(s, position, player)
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
            position = self.listening.position(s, player.get('video_id'))
            if position is None:
                return
            self.listening.record_player(s, position, player)
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
