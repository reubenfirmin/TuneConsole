"""Saved-playlist radio, observed through the extension's native playback state.

Start the selected song in a new YouTube playlist, then fill its queue after
playback begins. Ask Music's native refresh handler to fetch the saved
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
from yt_playlist.rec import measurement, radio_tuning
from yt_playlist.web.radio_edits import AppendOperation, SongContinuation
from yt_playlist.web.radio_launch import INITIAL_TRACKS, RadioLaunch, SeededCreateUncertain
from yt_playlist.web.radio_listening import RadioListening
from yt_playlist.web.radio_mutations import MutationResult, PlaylistTarget, RadioMutations
from yt_playlist.web.radio_playlist_tail import LOOKAHEAD, RadioPlaylistTail
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending, RadioPlaylistIO
from yt_playlist.web.radio_playback import RadioPlayback
from yt_playlist.web.radio_persistence import RadioPersistence, encode
from yt_playlist.web.radio_preparation import RadioPreparation
from yt_playlist.web.radio_projection import saved_view, status_view
from yt_playlist.web.radio_state import LaunchAdmission, Phase, PlaybackRecovery

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
        self.offer_now = None
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
        self.launch_receipts = self.persistence.launch_receipts()
        self.seeded_recovery = ctx.store.radio_checkpoints.pending_seeded_launch()
        self.cancelled_launches = OrderedDict()

    def _playing(self):
        """The extension's fresh playback presence is authoritative, not saved state."""
        return self.playback.current()

    def _is_current(self, s):
        return self.listening.current(s)

    def prepare(self):
        """Schedule preparation without waiting on session work or browser replies."""
        if self.ctx.store.activity_paused:
            return
        # Playback heartbeats call this from the WebSocket response pump. A busy
        # session can retry on the next heartbeat; it must not hold up its own replies.
        if not self.lock.acquire(blocking=False):
            return
        try:
            playing = self._playing().get("playlist_ytm_id")
            if self.session and self.session.seeded_launch and playing == self.session.playlist:
                self.session.wake.set()
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
                if self.ctx.store.activity_paused:
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
        menu_id, tracks = self._seed_offer(store)
        return {"state": state, "seeds": self._eligible_seeds(tracks, store=store),
                "seedMenu": menu_id,
                "playback": {"connected": self.ctx.bridge.connected, "now_playing": self._playing() or None}}

    def _seed_offer(self, store):
        tracks = self.offer
        if tracks is not None:
            return self.offer_id, tracks
        try:
            offer = json.loads(store.settings.get_setting('radio_seed_offer') or '{}')
        except (ValueError, TypeError):
            offer = {}
        return offer.get('menu_id'), offer.get('tracks', [])

    def starter_seed(self, menu_id, video_id):
        """Revalidate only the clicked song for the setup exception."""
        current_menu, tracks = self._seed_offer(self.ctx.store)
        if menu_id != current_menu:
            return False
        track = next((track for track in tracks if track.get('video_id') == video_id), None)
        return track is not None and bool(self._eligible_seeds([track]))

    def _persist(self, s):
        """Keep a resumable pointer using the existing settings API."""
        self.persistence.save(s, store=self.ctx.store, now=self.ctx.now_fn())

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
                    s = self.launch.resume(state, playing, clients, RadioCandidates)
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
            if (s and not s.stopped_by_user and not s.monitoring and not (s.seeded_launch and s.halted)
                    and self._is_current(s) and self.ctx.bridge.connected and self.work.accepting):
                s.stop = threading.Event()
                if s.playback_intent is None:
                    s.playback_intent = self.ctx.bridge.current_playback_intent()
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
        from yt_playlist.rec.radio_startup import eligible_seeds
        store = self.ctx.store if store is None else store
        return eligible_seeds(store, tracks, self.ctx.now_fn())

    def seeds(self):
        with self.operation:
            if not self._eligible_seeds(self.offer):
                candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn())
                self.offer, self.offer_id = self.listening.seed_offer(candidates)
                self.offer_now = None
                if getattr(candidates, 'lazy_modes', ()) and self.offer_id:
                    fields = ('encoding_model', 'group_generation', 'now_sampled_at', 'now_posterior',
                              'now_gain', 'group_allocation')
                    self.offer_now = SimpleNamespace(**{key: getattr(candidates, key) for key in fields})
                    candidates.close()
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
                launch.launch_id for request, launch in self.launch_requests.items()
                if launch.launch_id is not None and request.is_set()))
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

    def stop(self, *, launch_id=None):
        with self.lock:
            recovery = self.seeded_recovery
            if recovery and launch_id is not None and recovery['launch_id'] != launch_id:
                recovery = None
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
            for request, launch in self.launch_requests.items():
                if launch_id is None or launch.launch_id == launch_id:
                    request.set()
            if launch_id is None:
                self.tuning_serial += 1
        if recovery:
            self._take_seeded_recovery(recovery['launch_id'])
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
            s = self.session
            if s is None:
                return
            recovery = self.seeded_recovery
            if (recovery and recovery['episode_id'] == s.episode_id
                    and not s.monitoring and s.stop in self.launch_requests):
                # Let the in-flight Start record a proven rejection. Stop still
                # cancels its admission and removes its recovery record.
                return
            s.stop.set()
            s.wake.set()

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
                    if sent and self.session and self.session.episode_id == episode:
                        self.session.playback_intent = intent
                        self.session.playback_recovery = None
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
            prefix = self.policy.selection_prefix(tail.proposed, tail.resequence_after, history)
            mismatch = any(self.policy.genre_mismatch(view, history, t) for t in prefix)
            if tail.resequence_after is not None or mismatch:
                if mismatch:
                    prefix = [t for t in prefix if not self.policy.invalid(view, history, t)]
                    tail.choice_id = None
                tail.proposed = self.policy.replenish(view, history, prefix, tail.slot, excluded=tail.excluded)
                tail.resequence_after = None
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
            if position is None and edit.kind != 'add':
                # The removed song can still finish in native playback. Preserve
                # the confirmed successor until a real saved slot is playing.
                s.refresh_needed = True
                self.listening.observe(s, refresh_confirmed=True)
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
            if (not player or not player.get('video_id') or replacement.play_prefix is None
                    or video in s.directions.blocked
                    or video not in {t['video_id'] for t in replacement.proposed}
                    or video not in {t['video_id'] for t in s.tracks}):
                return
            # An uncertain delivery is never repeated. This live flag, like the
            # playback intent itself, is deliberately absent from checkpoints.
            play['sent'] = True
            if player.get('video_id') == video and not player.get('paused'):
                if self.ctx.bridge.playback_intent_current(play['intent']):
                    s.playback_intent = play['intent']
                return
            prior = player['video_id']
            if (position := self.listening.position(s, prior)) is not None:
                prior = s.tracks[position]['video_id']
            handoff = SongContinuation(video, replacement.choice_id, tuple(replacement.excluded), prior)
            try:
                sent = self.playback.navigate(s.playlist, video, intent=play['intent'])
                if sent:
                    s.playback_intent = play['intent']
                    s.playback_recovery = None
                    s.edits.handoff = handoff
                    logger.info("Radio song handoff: playlist=%s video=%s elapsed=%.3fs",
                                s.playlist, video,
                                time.monotonic() - play.get('requested_at', time.monotonic()))
                return sent
            except BridgeError:
                s.edits.handoff = handoff
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
        resequence_after = selection.get('resequence_after')
        tracks = self.policy.selection_prefix(selection['tracks'], resequence_after, history)
        mismatch = request is not None and any(
            self.policy.genre_mismatch(planning, history, track) for track in tracks)
        if resequence_after is not None or mismatch or (selection.get('play') and replacement is not None):
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
                if resequence_after is not None and (not tracks or tracks[0]['video_id'] != resequence_after):
                    choice = None
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
                # The published continuation may still be filling after the saved edit settles.
                source.continuation_excluded = set(replacement.excluded)
                source.revision += 1
                if not source.filling:
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
            # Observer progress can replace or clear the edit journal before
            # Apply gets the operation lock. The serial fences user actions.
            self.tuning_owner = (self.tuning_serial, ref(s) if s is not None else None,
                                 s.edits.song_continuation() if s is not None else None)
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
                admitted, owner, song = self.tuning_owner or (None, None, None)
                current = lambda: (s is self.session and serial == self.tuning_serial and s is not None
                                   and serial == admitted and (owner is None or owner() is s))
                if not current() or not self._is_current(s) or s.stop.is_set() or not s.directions:
                    logger.info("Radio tuning refused: reason=inactive_or_superseded serial=%d", serial)
                    return {"ok": False, "tune_reason": "superseded", **self.status()}
                unchanged = values == s.candidates.tuning
                previous_unconfirmed = self.tuning_unconfirmed
                self.tuning_unconfirmed = s
            # Reuse the prepared pools with the new source mix. This route runs
            # off the playback poll and never changes durable taste or play evidence.
            candidates = None
            owns_candidates = False
            applied = False
            try:
                if unchanged:
                    applied = True
                else:
                    with self.ctx.store.priority.request():
                        with cancellable_work(lambda: not current() or s.stop.is_set()):
                            retune = getattr(s.candidates, 'for_tuning', None)
                            candidates = retune(values) if retune else None
                            if candidates is None:
                                candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn(), values,
                                                             now_source=s.candidates)
                                owns_candidates = True
                        applied = self.listening.tune(s, candidates, current=current, song=song)
                if applied:
                    if not unchanged or previous_unconfirmed is s or self.persistence.transient is not None:
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
                if not applied and owns_candidates and candidates.prepared is not None:
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
            launch = LaunchAdmission(launch_id, requested_at=self.ctx.now_fn())
            self._expire_cancelled_launches()
            if launch_id in self.cancelled_launches:
                launch.cancelled.set()
            else:
                pending = (next((request for request in self.launch_requests.values()
                                 if request.launch_id == launch_id), None) if launch_id is not None else None)
                if pending is not None:
                    launch = replace(pending, replay=True, recover_seeded=False, recover_navigation=False)
                elif launch_id in self.launch_receipts:
                    launch.replay = True
                    recovery = self.seeded_recovery
                    if recovery and recovery['launch_id'] == launch_id:
                        launch.recover_seeded = not self._seeded_launch_established(recovery['episode_id'])
                        launch.recover_navigation = launch.recover_seeded and not recovery.get('dispatch_started')
                    if not launch.recover_seeded:
                        launch.finished.set()
                else:
                    # A distinct Start supersedes Apply before its worker runs.
                    self.tuning_serial += 1
            owns_request = not launch.replay or launch.recover_seeded
            if owns_request:
                self.launch_requests[launch.cancelled] = launch
        try:
            yield launch
        finally:
            if owns_request:
                with self.lock:
                    self.launch_requests.pop(launch.cancelled, None)
                    launch.finished.set()

    def start(self, seed_video_id=None, local_date=None, menu_id=None, *, playlist_id=None,
              intent=None, launch=None):
        with (nullcontext(launch) if launch is not None else self.launch_request()) as launch:
            if launch.recover_seeded:
                if intent is None and launch.recover_navigation and not launch.cancelled.is_set():
                    intent = self.ctx.bridge.begin_playback_intent()
                return self.recover_seeded_start(launch, intent)
            if launch.replay:
                return self.replay_start(launch)
            if intent is None and not launch.cancelled.is_set():
                intent = self.ctx.bridge.begin_playback_intent()
            try:
                with self.ctx.store.priority.request() as release:
                    return self._start(seed_video_id, local_date, menu_id, playlist_id=playlist_id,
                                       intent=intent, launch=launch, release_priority=release)
            except WorkClosing:
                launch.cancelled.set()
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

    def replay_start(self, launch):
        """Read the original result without reserving playback command order."""
        try:
            with cancellable_work(launch.cancelled.is_set):
                while not launch.finished.wait(.05):
                    check_stopping()
        except WorkClosing:
            return {'ok': False, 'reason': 'Stopped during start.', **self.status()}
        with self.lock:
            s = self.session
            episode = self.launch_receipts.get(launch.launch_id)
            if (self.seeded_recovery and self.seeded_recovery['episode_id'] == episode
                    and not self._seeded_launch_established(episode)):
                return {'ok': False, 'launch_outcome': 'uncertain', **self.status()}
            if s is not None and s.episode_id == episode and not launch.cancelled.is_set():
                if s.stopped_by_user or s.halted:
                    return {'ok': False, 'reason': s.error or 'Stopped during start.', **self.status()}
                return {'ok': True, 'joined': True, **self.status()}
        if s is None and episode is not None and not launch.cancelled.is_set():
            with self.ctx.store.read_view() as reader:
                try:
                    saved = self.persistence.view(reader)
                except (ValueError, KeyError, TypeError):
                    saved = None
            with self.lock:
                if (self.session is None and not launch.cancelled.is_set()
                        and saved and saved.get('episode_id') == episode):
                    if saved.get('stopped') or saved.get('halted'):
                        return {'ok': False, 'reason': saved.get('error') or 'Stopped during start.'}
                    active = bool(saved.get('playlist') and self._playing().get('playlist_ytm_id') == saved['playlist'])
                    return {'ok': True, 'joined': True, **saved_view(saved, active=active)}
                if self.session is None and not launch.cancelled.is_set() and saved is None:
                    # A reservation without a checkpoint does not prove the provider write failed.
                    return {'ok': False, 'launch_outcome': 'uncertain', **self.status()}
        return {'ok': False, 'reason': 'This Radio start has already been handled.', **self.status()}

    def _seeded_launch_established(self, episode):
        s = self.session
        return ((self.persistence.revision is not None and self.persistence.revision[0] == episode)
                or (s is not None and s.episode_id == episode and s.monitoring and not s.halted))

    def recover_seeded_start(self, launch, intent):
        cancelled = launch.cancelled
        pending = {'ok': False, 'launch_outcome': 'uncertain'}
        stopped = {'ok': False, 'reason': 'Stopped during start.'}
        try:
            with cancellable_work(cancelled.is_set), self._launch_operation(cancelled):
                with self.lock:
                    recovery = self.seeded_recovery
                    if not recovery or recovery['launch_id'] != launch.launch_id:
                        return {**stopped, **self.status()}
                if not self.ctx.bridge.connected:
                    return {**pending, **self.status()}
                try:
                    dispatch_started = recovery.get('dispatch_started')
                    playlist = recovery.get('playlist')
                    if dispatch_started and not self._playing_seed(recovery, playlist):
                        return {**pending, **self.status()}
                    _, client = client_for(self.ctx.store, self.ctx.client_provider(), account_id=recovery['identity'])
                    s = self.launch.restore_seeded(recovery, client, RadioCandidates)
                    if not playlist:
                        playlist = self.launch.find_seeded(s)
                        if playlist and not self._update_seeded_dispatch(recovery, playlist, cancelled, started=False):
                            return {**pending, **self.status()}
                except WorkClosing:
                    raise
                except Exception as exc:
                    logger.debug('Radio create readback deferred: %s', type(exc).__name__)
                    return {**pending, **self.status()}
                if not playlist or not self.ctx.bridge.connected:
                    return {**pending, **self.status()}
                already_playing = self._playing_seed(recovery, playlist)
                if already_playing and not dispatch_started:
                    if not self._update_seeded_dispatch(recovery, playlist, cancelled, started=True):
                        return {**pending, **self.status()}
                    dispatch_started = True
                with self.lock:
                    if cancelled.is_set() or self.seeded_recovery is not recovery:
                        return {**stopped, **self.status()}
                    already_playing = already_playing and self._playing_seed(recovery, playlist)
                    if dispatch_started and not already_playing:
                        return {**pending, **self.status()}
                    if self.session:
                        self.session.stop.set()
                    self.session = s
                    s.stop = cancelled
                    s.playback_intent = intent
                    s.trace_launch_id = launch.launch_id if launch.trace else None
                    self.launch.accept_seeded(s, playlist)
                    s.playback_confirmed = already_playing
                if not already_playing and not self._handoff_seeded(s, recovery):
                    with self.lock:
                        s.playlist = None
                        if not s.stopped_by_user:
                            s.move(Phase.CONFLICT)
                    return {**(stopped if cancelled.is_set() else pending), **self.status()}
                if cancelled.is_set():
                    return {**stopped, **self.status()}
                self._record_seed_choice(s, recovery['requested_at'])
                s.wake.set()
                self._start_monitor(s)
                return {'ok': True, 'joined': True, **self.status()}
        except WorkClosing:
            return {**stopped, **self.status()}

    def _playing_seed(self, recovery, playlist):
        playing = self._playing()
        return bool(playlist and playing.get('playlist_ytm_id') == playlist
                    and playing.get('video_id') == recovery['seed']['video_id'])

    def _update_seeded_dispatch(self, recovery, playlist, cancelled, *, started, was_started=False):
        if cancelled.is_set():
            return False
        if not self.ctx.store.radio_checkpoints.update_seeded_dispatch(
                recovery['launch_id'], playlist, started=started, was_started=was_started):
            return False
        with self.lock:
            if self.seeded_recovery is recovery and not cancelled.is_set():
                recovery.update(playlist=playlist, dispatch_started=started)
                return True
        return False

    def _take_seeded_recovery(self, launch_id):
        claimed = self.ctx.store.radio_checkpoints.take_seeded_launch(launch_id)
        with self.lock:
            if self.seeded_recovery and self.seeded_recovery['launch_id'] == launch_id:
                self.seeded_recovery = None
        return claimed

    def _reserve_launch(self, launch, session, *, seeded_recovery=None):
        cancelled = launch.cancelled
        reserved = False
        try:
            with cancellable_work(cancelled.is_set):
                with self.lock:
                    recovery = self.seeded_recovery
                    clear_seeded = recovery is not None and recovery['episode_id'] != session.episode_id
                reserved = self.persistence.reserve_launch(launch.launch_id, session.episode_id,
                    clear_seeded=clear_seeded, seeded_recovery=seeded_recovery)
                if reserved:
                    with self.lock:
                        if seeded_recovery is not None or clear_seeded:
                            self.seeded_recovery = seeded_recovery if not cancelled.is_set() else None
                        if launch.launch_id is not None:
                            self.launch_receipts[launch.launch_id] = session.episode_id
        finally:
            # Stop may run between the commit and publishing the live reservation.
            if reserved and seeded_recovery is not None and cancelled.is_set():
                self._take_seeded_recovery(launch.launch_id)
        return reserved

    def _start(self, seed_video_id, local_date, menu_id, *, playlist_id, intent, launch, release_priority):
        cancelled = launch.cancelled
        # With no native playlist there is nothing to restore or adopt for an
        # explicit selection. Persistence already holds the checkpoint revision.
        playing = self._playing().get('playlist_ytm_id')
        if playlist_id is None and (not seed_video_id or (playing and (playing != self.checked_playlist or self.active))):
            with cancellable_work(cancelled.is_set):
                self.join(cancelled=cancelled)
        with self._launch_operation(cancelled):
            day = date.fromisoformat(local_date) if local_date else datetime.fromtimestamp(self.ctx.now_fn()).date()
            with self.lock:
                s = self.session
                joined = playlist_id is None and self._is_current(s)
            if joined:
                if not self._reserve_launch(launch, s):
                    return {'ok': False, 'reason': 'This Radio start has already been handled.'}
                with self.lock:
                    # A distinct join can take ownership of a handoff that is
                    # still unsent. A replay exits before reserving an intent.
                    if s.playback_intent is not None and not s.launch_sent and not s.playback_confirmed:
                        s.playback_intent = max(s.playback_intent, intent)
                        self._handoff_start(s)
                    return {"ok": True, "joined": True, **self.status()}
            if not self.ctx.bridge.connected:
                return {"ok": False, "reason": "Connect the YouTube Music extension first."}
            try:
                candidates = None
                try:
                    # Cancellation applies to disposable reads and planning.
                    # Once creation starts, uncertain writes still drain through
                    # their existing confirmation and checkpoint protocol.
                    with cancellable_work(cancelled.is_set), work_span('radio: resolve startup inputs', announce=True):
                        now_source = self.offer_now if (menu_id or self.offer_id) == self.offer_id else None
                        store = self.ctx.store
                        clients = self.ctx.client_provider()
                        known = bool(seed_video_id or playlist_id is not None)
                        source_queue = None
                        s = None
                        if known:
                            with store.read_view(seconds=None) as reader:
                                identity, client = client_for(reader, clients)
                                if playlist_id is not None:
                                    source_queue = reader.radio_catalog.playlist_queue(playlist_id)
                                    if source_queue is None:
                                        raise ValueError("That playlist no longer exists. Choose another playlist.")
                                    if not source_queue:
                                        raise ValueError("This playlist has no playable songs. Choose another playlist.")
                                s = self.launch.resolve_known(reader, client, identity, day,
                                    source_queue=source_queue, playlist_id=playlist_id,
                                    seed_video_id=seed_video_id, menu_id=menu_id or self.offer_id,
                                    offer=self.offer if now_source is not None else None, factory=RadioCandidates)
                        else:
                            identity, client = client_for(store, clients)
                        if s is None:
                            candidates = RadioCandidates(store, self.ctx.now_fn(), now_source=now_source,
                                                         initial_queue=known)
                            s = self.launch.resolve(candidates, client, identity, day, source_queue=source_queue,
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
                self.offer_now = None
                return {"ok": False, "refresh_seeds": True, **self.status()}
            recovery = None
            if s.seeded_launch and launch.launch_id is not None:
                recovery = {'launch_id': launch.launch_id, 'episode_id': s.episode_id,
                            'identity': s.identity, 'title': s.title, 'seed': s.selected[0],
                            'choices': s.choices, 'started_at': s.started_at, 'requested_at': launch.requested_at}
            try:
                reserved = self._reserve_launch(launch, s, seeded_recovery=recovery)
            except BaseException:
                s.candidates.close()
                raise
            if not reserved:
                s.candidates.close()
                return {'ok': False, 'reason': 'This Radio start has already been handled.'}
            with self.lock:
                if cancelled.is_set():
                    s.candidates.close()
                    return {'ok': False, 'reason': 'Stopped during start.'}
                if self.session:
                    self.session.stop.set()
                s.stop = cancelled
                s.launch_id = launch.launch_id
                s.trace_launch_id = launch.launch_id if launch.trace else None
                s.playback_intent = intent
                self.session = s
            try:
                if s.seeded_launch:
                    try:
                        playlist = self.launch.create_seeded(s)
                    except SeededCreateUncertain:
                        raise
                    except Exception:
                        if recovery is not None:
                            self._take_seeded_recovery(launch.launch_id)
                        raise
                    if recovery is not None and not self._update_seeded_dispatch(recovery, playlist, cancelled, started=False):
                        return {'ok': False, 'reason': 'Stopped during start.', **self.status()}
                    self.launch.accept_seeded(s, playlist)
                    if recovery is None:
                        with self.lock:
                            self._handoff_start(s)
                    elif not self._handoff_seeded(s, recovery):
                        if cancelled.is_set():
                            return {'ok': False, 'reason': 'Stopped during start.', **self.status()}
                        with self.lock:
                            if self.seeded_recovery is recovery:
                                raise SeededCreateUncertain('YouTube Music did not confirm the playback handoff.')
                    if cancelled.is_set():
                        return {'ok': False, 'reason': 'Stopped during start.', **self.status()}
                    release_priority()
                    self._record_seed_choice(s, launch.requested_at)
                else:
                    if seed_video_id and playlist_id is None:
                        self._record_seed_choice(s, launch.requested_at)
                    if not self.launch.create(s, from_playlist=playlist_id is not None):
                        return {"ok": False, "reason": "Stopped during start."}
                    if not s.stop.is_set():
                        try:
                            self._complete_start(s, release_priority=release_priority)
                        except PlaylistPending:
                            self._persist(s)
                if s.stop.is_set():
                    self._persist(s)
                    return {"ok": False, "reason": "Stopped during start."}
                if s.seeded_launch or s.initial_queue_maximum is not None or getattr(s.candidates, 'lazy_modes', ()):
                    s.wake.set()
                self._start_monitor(s)
            except SeededCreateUncertain as exc:
                logger.warning('Radio seeded start unconfirmed: launch=%r', launch.launch_id, exc_info=True)
                with self.lock:
                    if not s.stopped_by_user:
                        s.move(Phase.CONFLICT)
                if not cancelled.is_set():
                    return {'ok': False, 'launch_outcome': 'uncertain', 'reason': str(exc), **self.status()}
                return {'ok': False, 'reason': 'Stopped during start.', **self.status()}
            except Exception as exc:
                logger.warning("Radio Session start failed", exc_info=True)
                with self.lock:
                    s.error = str(exc)
                    if not s.stopped_by_user:
                        s.move(Phase.CONFLICT)
                    recoverable = recovery is not None and self.seeded_recovery is recovery and not cancelled.is_set()
                    if not recoverable:
                        s.stop.set()
                if recoverable:
                    return {'ok': False, 'launch_outcome': 'uncertain', **self.status()}
                try:
                    self._persist(s)
                except Exception:
                    logger.warning("Radio start checkpoint could not be saved", exc_info=True)
                return {"ok": False, "reason": str(exc), **self.status()}
            finally:
                if s.seeded_launch and s.playlist and not s.monitoring:
                    self.launch.retain_seeded(s.playlist)
                with self.lock:
                    s.settle()
                if s.local_id is not None and s.save_effects:
                    self._record_launch_save(s)
            return {"ok": True, **self.status()}

    def _record_launch_save(self, s):
        keys, s.save_effects = s.save_effects, ()
        try:
            self.work.submit(measurement.record_action, self.ctx.store, 'save', self.ctx.now_fn(),
                             keys=keys, event_id=f'generate:{s.playlist}', generated=True, prune=False)
        except WorkClosing:
            pass

    def _record_seed_choice(self, s, requested_at):
        choice = s.choices[s.selected[0]['video_id']]
        if choice['menu_id'] == 'direct':
            return
        args = (choice['menu_id'], choice['option_id'], s.episode_id,
                s.started_at if requested_at is None else requested_at)
        if getattr(self.ctx.store, 'choice_capture', None) is None:
            self.launch.record_seed_choice(*args)
        else:
            self.work.submit(self.launch.record_seed_choice, *args)

    def _complete_start(self, s, *, release_priority=None):
        prepared = self.launch.confirm(s)
        with work_span('radio: publish confirmed startup', announce=True):
            with self.lock:
                self.launch.accept(s, prepared)
            self._persist(s)
        with self.lock:
            self._handoff_start(s)
        if release_priority is not None:
            release_priority()
        # The adaptive monitor observes playback and builds its first menu
        # from the confirmed queue and the song the player actually started.
        if s.initial_queue_maximum is None and not getattr(s.candidates, 'lazy_modes', ()):
            self.listening.initial_directions(s)
            self._persist(s)

    def _handoff_seeded(self, s, recovery):
        claimed = False

        def prepare():
            nonlocal claimed
            claimed = self._update_seeded_dispatch(recovery, s.playlist, s.stop, started=True)
            return claimed

        try:
            if self._handoff_start(s, prepare=prepare):
                return True
        except BridgeError as exc:
            with self.lock:
                s.launch_sent = False
            if exc.outcome != 'not-dispatched':
                return False
        # Only a definite pre-queue rejection can release the dispatch claim.
        if not self.ctx.bridge.playback_intent_current(s.playback_intent):
            self._take_seeded_recovery(recovery['launch_id'])
        elif claimed:
            self._update_seeded_dispatch(recovery, s.playlist, s.stop, started=False, was_started=True)
        return False

    def _handoff_start(self, s, *, prepare=None):
        """Send the confirmed initial queue once; only a rejected intent may retry.

        A queued or uncertain delivery must never be repeated by a joining
        request, preparation retry or restored session.
        """
        with self.lock:
            if (s is not self.session or s.stop.is_set() or s.launch_sent
                    or s.playback_confirmed or s.playback_intent is None
                    or s.initial_tracks or not s.tracks):
                return False
            s.launch_sent = True
            playlist, video, intent = s.playlist, s.tracks[0]['video_id'], s.playback_intent
        sent = self.playback.navigate(playlist, video, intent=intent, launch_id=s.trace_launch_id,
                                      prepare=prepare, cancelled=s.stop.is_set)
        if sent is False:
            with self.lock:
                s.launch_sent = False
            return False
        if s.trace_launch_id:
            logger.info('Radio launch timing: launch=%r stage=navigate_queued playlist=%s video=%s',
                        s.trace_launch_id, playlist, video)
        return True

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

    def skip(self, video_id, *, brand_id=None, rating_advance=False):
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
                            # Retain the request even if playback needs the saved successor.
                            from yt_playlist.core import playback_feedback
                            playback_feedback.record_next(self.ctx.store,
                                s.identity, video_id, player.get('position'),
                                player.get('duration'), s.playlist, self.ctx.now_fn(),
                                rating_advance=rating_advance)
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
                                if rating_advance:
                                    options['rating_advance'] = True
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
                            s.playback_intent = self.ctx.bridge.begin_playback_intent()
                            s.playback_recovery = None
                            self.playback.navigate(s.playlist, successor, intent=s.playback_intent)
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
                serial = self.tuning_serial

                def planning_cancelled():
                    return serial != self.tuning_serial or self.tuning_pending is not None or s.stop.is_set()

                try:
                    if s.playback_confirmed and not s.seeded_launch and not song_requested():
                        self._refresh_now(s)
                    with self.operation:
                        if s is not self.session or s.stop.is_set():
                            break
                        if s.seeded_launch and s.furthest < 0 and time.monotonic() >= s.preparation_deadline:
                            raise PlaylistChanged('YouTube Music did not confirm the starting playlist.')
                        if planning_cancelled():
                            continue
                        if s.initial_tracks:
                            self._complete_start(s)
                            continue  # The completed start already saved its checkpoint.
                        else:
                            try:
                                self.listening.observe(s, planning_cancelled=planning_cancelled)
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
                            if planning_cancelled():
                                continue
                            if not s.seeded_launch and not song_requested():
                                self.listening.refresh_metadata(s)
                            if planning_cancelled():
                                continue
                            if not s.seeded_launch and not song_requested():
                                self._refresh_candidates(s)
                            try:
                                self._advance(s, planning_cancelled=planning_cancelled)
                            finally:
                                if not song_requested():
                                    self.listening.fill_tuned_streams(s, cancelled=planning_cancelled)
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
                        if s.seeded_launch:
                            s.exhausted = True
                        if s.seeded_launch or not s.playback_confirmed:
                            s.stop.set()
                    self._persist(s)
                except WorkClosing:
                    if self.work.accepting and not s.stop.is_set() and planning_cancelled():
                        continue
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
            if s.seeded_launch and s.local_id is None:
                self.launch.retain_seeded(s.playlist)
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

    def _advance(self, s, *, planning_cancelled=None):
        if s.stop.is_set() or planning_cancelled is not None and planning_cancelled():
            return
        if self._recover_playback(s):
            return
        if self.ctx.store.transient_radio_playlists and not self.ctx.store.activity_paused:
            if s.candidates.prepared is not None:
                s.candidates.prepared.retain(self.ctx.now_fn())
            self.launch.retain_provenance()
        if s.seeded_launch and not self._finish_seeded_launch(s, planning_cancelled):
            return
        player = self.listening.player(s)
        if player is None:
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
        position = self.listening.record_queue_position(s, player)
        if position is None:
            return
        changed = self.listening.feedback(s)
        if position < s.furthest:
            self.listening.update(s, force=changed, cancelled=planning_cancelled)
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
            excluded = s.directions.continuation_excluded
            proposal = self.policy.replenish(s.candidate_view(), history, valid, s.directions.active, excluded=excluded)
            s.edits.begin(RadioPlaylistTail(s.tracks, proposal, s.directions.active, excluded=excluded))
            if not self._finish_replacement(s):
                return
        self.listening.update(s, force=changed, cancelled=planning_cancelled)
        # A bounded pass keeps Stop responsive even when someone skips rapidly.
        for _ in range(LOOKAHEAD + 1):
            player = self.listening.player(s)
            if s.stop.is_set() or player is None:
                return
            position = self.listening.record_queue_position(s, player)
            if position is None:
                return
            self.listening.update(s, cancelled=planning_cancelled)
            if len(s.tracks) - position - 1 >= LOOKAHEAD:
                s.error = None
                return
            if not self._append(s):
                return

    def _recover_playback(self, s):
        """Give Music time to repair its queue before navigating to a saved successor."""
        if (s.skip_requested or s.halted or s.requested_direction and s.requested_direction.get('play')
                or s.replacement and s.replacement.play):
            return False
        player = self.listening.player(s, allow_paused=True)
        if not player or not player.get('video_id'):
            return False
        error = player.get('error')
        if not error and not player.get('ended'):
            s.playback_recovery = None
            return False
        now = time.monotonic()
        with self.lock:
            intent = s.playback_intent
            if (s is not self.session or s.stop.is_set() or intent is None
                    or not self.ctx.bridge.playback_intent_current(intent)):
                return False
            position = self.listening.recovery_position(s, player)
            video = player['video_id']
            # A finished or failed native upload can have no saved counterpart.
            # Continue this playlist from its confirmed played prefix in that case.
            after = s.furthest if position is None else position
            saved = s.tracks[position]['video_id'] if position is not None else None
            recovery = s.playback_recovery
            if recovery is not None and position is None:
                # A changed provider ID can be the result of the retry itself.
                # Keep the same retry window and guard navigation with its latest ID.
                recovery.video = video
            elif recovery is None or recovery.video != video:
                recovery = PlaybackRecovery(video, now + 4, successors=recovery.successors if recovery else set())
                s.playback_recovery = recovery
                s.refresh_needed = True
                logger.info("Radio recovery waiting for native queue: playlist=%s saved=%s current=%s "
                            "after_index=%s reason=%s", s.playlist, saved, video, after, error or "ended")
                return True
            if now < recovery.ready_at:
                return True
            retry = bool(error and not recovery.retried)
            target = video if retry else next(
                (t['video_id'] for t in s.tracks[after + 1:] if t['video_id'] not in recovery.successors), None)
            if target:
                # Allow the extension's normal navigation and playback rescue
                # to settle. A later observation, rather than send, proves play.
                recovery.ready_at = now + 12
                try:
                    sent = self.playback.navigate(s.playlist, target, intent=intent, failed_video=video)
                except BridgeError as exc:
                    logger.info("Radio recovery navigation unconfirmed: playlist=%s target=%s", s.playlist, target)
                    sent = exc.outcome != 'not-dispatched'
                if sent:
                    if retry:
                        recovery.retried = True
                    else:
                        recovery.successors.add(target)
                    logger.info("Radio recovery %s: playlist=%s saved=%s current=%s after_index=%s target=%s reason=%s",
                                "retry" if retry else "skip", s.playlist, saved, video, after, target, error or "ended")
                return True
        if not s.exhausted and not s.replacement and not s.requested_direction:
            self._append(s)
        return True

    def _finish_seeded_launch(self, s, cancelled):
        # Once playing was observed, the same playlist can finish filling while paused.
        player = self.listening.player(s, allow_paused=True) if s.furthest >= 0 else None
        if player is None and (s.playback_confirmed or s.pending is not None or time.monotonic() < s.launch_deadline):
            return False
        if player is not None:
            for _ in range(INITIAL_TRACKS - len(s.tracks)):
                if s.stop.is_set() or cancelled is not None and cancelled():
                    return False
                if not self._append(s):
                    break
            if s.pending is not None or (len(s.tracks) < INITIAL_TRACKS and not s.exhausted):
                return False
        self.launch.publish_seeded(s)
        self._persist(s)
        if s.launch_id is not None and self.persistence.revision and self.persistence.revision[0] == s.episode_id:
            self._take_seeded_recovery(s.launch_id)
        s.initial_readback = None
        s.seeded_launch = False
        if s.local_id is None:
            self.launch.retain_seeded(s.playlist)
        elif s.save_effects:
            self._record_launch_save(s)
        return True

    def _append(self, s):
        if s.pending is None:
            track = self.policy.next_track(s.candidate_view())
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
                # A quick playback transition can append before the menu finishes filling.
                if not s.directions.filling:
                    s.directions.continuation_excluded.clear()
                s.exhausted = False
                s.error = None
                s.refresh_needed = True
                if s.seeded_launch:
                    s.initial_readback = result.snapshot
            self._persist(s)
            if not s.seeded_launch:
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
