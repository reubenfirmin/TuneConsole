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

from yt_playlist.core.activity import activity_for, cancellable_work, check_stopping
from yt_playlist.core.accounts import client_for
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.library import executor
from yt_playlist.library.playlist_metadata import versioned_title
from yt_playlist.rec import measurement
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_policy import RadioPolicy
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec import radio_tuning
from yt_playlist.web.radio_edits import AppendOperation
from yt_playlist.web.radio_mutations import MutationResult, PlaylistTarget, RadioMutations
from yt_playlist.web.radio_playlist_tail import LOOKAHEAD, RadioPlaylistTail, TailView
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending, RadioPlaylistIO
from yt_playlist.web.radio_playback import (PlaybackObservation, PlaybackRequest, PlaybackUnavailable, RadioPlayback)
from yt_playlist.web.radio_persistence import PREPARATION_TIMEOUT, RadioPersistence, restore
from yt_playlist.web.radio_projection import saved_view, status_view
from yt_playlist.web.radio_state import Phase, Session, selected_slot

logger = logging.getLogger(__name__)
INITIAL_TRACKS = 3
PREPARATION_SETTLE = 4


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
        self.preparing = False
        self.checked_playlist = None

    def _playing(self):
        """The extension's fresh playback presence is authoritative, not saved state."""
        return self.playback.current()

    def _is_current(self, s):
        if not s or s.stopped_by_user or (s.halted and not s.playback_confirmed):
            return False
        playing = self._playing()
        playlist = (playing.get("playlist_ytm_id") or "").removeprefix("RDAMPL")
        if s.playlist and playlist == s.playlist:
            s.launch_deadline = 0
            s.playback_confirmed = True
            return True
        # Missing heartbeats, pauses and reconnects do not end an established
        # session. A fresh report of another playlist does leave this radio.
        owned = {t["video_id"] for t in s.tracks}
        # The media heartbeat can briefly carry the previous playlist context
        # during Music's native refresh. A song from this session is still radio.
        if playlist and s.playback_confirmed and playing.get("video_id") not in owned:
            s.playback_confirmed = False
            return False
        # Only an explicit start in this process gets a short navigation grace.
        # Restoring a saved session never launches playback or receives this grace.
        return s.playback_confirmed or s.starting or time.monotonic() < s.launch_deadline

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
        if self.session is not None:
            state = self.status()
        else:
            try:
                saved = self.persistence.load() or {}
            except (ValueError, KeyError, TypeError):
                saved = {}
            active = bool(saved.get("playlist") and self._playing().get("playlist_ytm_id") == saved["playlist"]
                          and not saved.get("stopped"))
            state = saved_view(saved, active=active)
            # A checkpoint alone is not evidence that radio is playing. Heartbeat
            # gaps must not replace the seed grid with a speculative restore loader.
            # A confirmed playing session waits for join to restore its full view.
            state["resolving"] = active
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
        """Attach the page to the saved session without navigation or playlist creation."""
        with self.lock:
            s = self.session
            if s and (s.monitoring or s.stopped_by_user or not self.ctx.bridge.connected):
                return self.status()
        with self.operation:
            if self.session is None:
                try:
                    state = self.persistence.load()
                    if state:
                        # Stop ends ownership even while YouTube keeps playing.
                        # Legacy adoption must not revive that stopped checkpoint.
                        if state.get("stopped") and state.get("playlist") == self._playing().get("playlist_ytm_id"):
                            return self.status()
                        clients = self.ctx.client_provider() or {}
                        # JSON can round-trip a numeric identity as a string in some
                        # older settings; match the provider's actual identity key.
                        identity = next((key for key in clients if str(key) == str(state["identity"])), None)
                        if (identity is not None
                                and not state.get("stopped")
                                and state.get("playlist") and state["playlist"] == self._playing().get("playlist_ytm_id")
                                and (state.get("tracks") or state.get("initial_tracks"))):
                            candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn(), state.get("tuning"))
                            s = restore(state, clients[identity], identity, candidates)
                            self.session = s
                            if s.tracks:
                                self._update_directions(s, force=not s.replacement and
                                    state.get("fingerprint") != candidates.fingerprint)
                except (ValueError, KeyError, TypeError):
                    logger.warning("Could not restore the saved Radio Session", exc_info=True)
                if self.session is None:
                    self._adopt_playing()
            s = self.session
            if (s and not s.stopped_by_user and not s.monitoring
                    and self._is_current(s) and self.ctx.bridge.connected and self.work.accepting):
                s.stop = threading.Event()
                self._start_monitor(s)
        return self.status()

    def _adopt_playing(self):
        """Attach an already playing Radio Session created before resumable state existed."""
        now_playing = self._playing()
        playlist_id = now_playing.get("playlist_ytm_id")
        known = json.loads(self.ctx.store.settings.get_setting("radio_session_playlist_ids") or "[]")
        if not playlist_id or playlist_id not in known:
            return
        local = next((p for p in self.ctx.store.playlists.get_playlists() if p.ytm_playlist_id == playlist_id), None)
        if local is None:
            return
        client = (self.ctx.client_provider() or {}).get(local.identity_id)
        if client is None:
            return
        candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn())
        candidates.retain([{**track, "key": track["identity_key"]}
            for track in self.ctx.store.charts.playlist_tracks_detail(local.id)])
        s = Session(client, candidates, local.identity_id, local.title)
        s.playlist, s.local_id = playlist_id, local.id
        ids = self._remote_ids(s)
        if not ids or any(video not in candidates.by_video for video in ids):
            return
        s.tracks = [candidates.by_video[video] for video in ids]
        s.selected = list(s.tracks)
        s.position = next((i for i, track in enumerate(s.tracks)
                           if track["video_id"] == now_playing.get("video_id")), -1)
        s.furthest = s.position
        s.move(Phase.AWAITING_PLAYBACK)
        s.playback_confirmed = True
        s.settle()
        s.started_at = self.ctx.now_fn()
        s.directions = RadioDirections(candidates, s.tracks[0])
        self.session = s
        self._mirror(s, s.tracks)
        self._update_directions(s)
        self._persist(s)

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
                self.offer = candidates.seed_offer()
                self.offer_id = uuid4().hex
                measurement.record_recommendations(self.ctx.store, "radio_seed", self.offer,
                    self.ctx.now_fn(), request_id=self.offer_id, ranker="radio-seed",
                    provenance={"menu_id": self.offer_id, "fingerprint": candidates.fingerprint,
                                "choice_unit": "radio_session"})
                self.ctx.store.settings.set_setting("radio_seed_offer",
                    json.dumps({"tracks": self.offer, "menu_id": self.offer_id}))
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
        """Apply confirmed state and attribution; transaction I/O belongs to RadioMutations."""
        for substitution in result.substitutions:
            requested, accepted = substitution.requested, substitution.accepted
            s.candidates.retain([accepted])
            s.rejected.add(requested['video_id'])
            if s.directions:
                s.directions.blocked.add(requested['video_id'])
            choice = s.choices.pop(requested['video_id'], None)
            if choice:
                s.choices[accepted['video_id']] = choice
        if result.tracks is not None:
            self._accept_tracks(s, result.tracks)

    def _mirror(self, s, tracks):
        """Atomically publish confirmed contents, retaining an unchanged prefix."""
        self.playlists.publish(s.local_id, s.title, s.tracks, tracks, self.ctx.now_fn())

    def _remote_tracks(self, s, *, expected=None):
        return self.playlists.read(s.client, s.playlist, expected=expected)

    def _remote_ids(self, s):
        return [t["videoId"] for t in self._remote_tracks(s)]

    def _publish(self, s, tracks):
        """Only complete, confirmed remote reads can become the local playlist UI."""
        if [t["video_id"] for t in tracks] != [t["video_id"] for t in s.tracks]:
            self._mirror(s, tracks)
            self._accept_tracks(s, tracks)

    def _accept_tracks(self, s, tracks):
        if [t['video_id'] for t in tracks] != [t['video_id'] for t in s.tracks]:
            with self.lock:
                now = self.ctx.now_fn()
                s.edits.recent_states = [state for state in s.edits.recent_states if now - state["at"] < 60]
                s.edits.recent_states.append({"ids": [t["video_id"] for t in s.tracks], "at": now})
                current = s.tracks[s.position]["video_id"] if s.position >= 0 else None
                past = {t["video_id"] for t in s.tracks[:s.furthest + 1]}
                s.position = next((i for i, t in enumerate(tracks) if t["video_id"] == current), -1)
                s.furthest = max((i for i, t in enumerate(tracks) if t["video_id"] in past), default=-1)
                s.tracks = list(tracks)
                s.selected = list(tracks)

    def _browser(self, s):
        # A replacement can involve several saved-playlist edits. Only ask the
        # native player to fetch the playlist after the final state is saved.
        refresh = s.refresh_needed and not s.requested_direction and (not s.replacement or s.replacement.saved)
        refresh_at = self.ctx.now_fn() if refresh else None
        try:
            observation = self.playback.observe(PlaybackRequest(
                s.playlist, tuple(t["video_id"] for t in s.tracks), refresh))
        except PlaybackUnavailable as exc:
            s.observer_error = str(exc)
            logger.debug("Radio observer reconnecting: %s", exc)
            raise PlaylistPending() from exc
        result = observation.state
        with self.lock:
            s.observer_error = None
            if not s.halted:
                s.error = None
            s.browser = result
            s.browser_seen_at = observation.seen_at
            player = result.get("player") or {}
            if (player.get("playlist_ytm_id") == s.playlist and player.get("video_id")
                    and not player.get("paused")):
                s.playback_confirmed = True
                s.launch_deadline = 0
            # A refresh request alone is not acknowledgement. Keep checking the
            # actual future, including if a late response restores an older one.
            s.refresh_needed = not bool(result.get("playback_synced"))
            if refresh_at is not None and result.get("refresh") in {"clicked", "requested"}:
                s.edits.windows.append([refresh_at, self.ctx.now_fn() + 3])
            self._report_playback(s, result)
        # Playback observation must not wait for a playlist mutation to settle.
        # Otherwise a halted/uncertain tail leaves the playing song in proposals.
        position = next((i for i, track in enumerate(s.tracks)
                         if player.get("playlist_ytm_id") == s.playlist
                         and track["video_id"] == player.get("video_id")), None)
        if position is not None and (position != s.position or s.observed_video != player.get("video_id")):
            self._observe(s, position)
            self._update_directions(s)
        elif position is None and player.get("playlist_ytm_id") == s.playlist:
            video = player.get("video_id")
            # A saved suffix write can finish after native playback entered the
            # removed song. Only accept uploads previously owned by this session.
            owned = {t["video_id"] for t in s.heard}
            owned.update(video for state in s.edits.recent_states for video in state["ids"])
            if video in owned and video in s.candidates.by_video:
                self._observe(s, -1, track=s.candidates.by_video[video])
                self._update_directions(s)

    def _report_playback(self, s, result):
        """Log changed native state once; routine two-second polls stay quiet."""
        player = result.get("player") or {}
        native = result.get("native") or {}
        video = player.get("video_id")

        def following(ids):
            index = ids.index(video) + 1 if video in ids else 0
            return tuple(ids[index:index + 3])

        saved = following([t["video_id"] for t in s.tracks])
        loaded = following(native.get("videoIds") or [])
        embedded = following(player.get("loadedVideoIds") or [])
        report = (result.get("observerVersion"), player.get("playlist_ytm_id"), video,
                  saved, loaded, embedded, result.get("playback_synced"),
                  result.get("refresh"), result.get("autoplay"), native.get("available"))
        refresh_state = result.get("refresh")
        if refresh_state in {"clicked", "requested", "waiting", "waiting for native playlist refresh"}:
            refresh_state = "pending"
        elif refresh_state in {"confirmed", "not requested"}:
            refresh_state = None
        signature = report[:7] + report[8:] + (refresh_state,)
        if signature == s.observer_report and result.get("refresh") not in {"clicked", "requested"}:
            return
        s.observer_report = signature
        logger.info("Radio playback: observer=%s playlist=%s current=%s saved_next=%s "
                    "native_next=%s embedded_next=%s synced=%s refresh=%s autoplay=%s native_available=%s",
                    *report)

    def _player(self, s):
        if s.browser_seen_at is None:
            return None
        return PlaybackObservation(s.browser or {}, s.browser_seen_at).player(s.playlist, allow_paused=s.skip_requested)

    def _observe(self, s, position, *, track=None):
        track = track if track is not None else s.tracks[position]
        video = track["video_id"]
        now = self.ctx.now_fn()
        with self.lock:
            s.position = position
            s.furthest = max(s.furthest, position)
            if s.directions:
                s.directions.retired = {t["video_id"] for t in s.tracks[:s.furthest + 1] + s.heard} | {video}
            if s.observed_video == video:
                return
            if s.observed_video:
                s.transition += 1
            s.observed_video = video
            s.starts[video] = now
            s.heard.append(track)
            choice = s.choices.pop(video, None)
        # Count an explicit seed/branch choice once when its first song starts.
        # Passive continuation tracks remain generated listening in the live-play pipeline.
        if choice:
            measurement.record_action(self.ctx.store, "start", now,
                keys=[track["key"]],
                event_id=f"radio:{s.episode_id}:{choice['menu_id']}:{choice['id']}")

    def _update_directions(self, s, *, force=False):
        with self.lock:
            self.policy.update_directions(s.candidate_view(), s.history_view(), force=force or s.feedback_dirty)
            s.feedback_dirty = False

    def _feedback(self, s):
        update = self.policy.feedback(s.candidate_view(), s.feedback_context())
        s.accept_feedback(update)
        return update.changed

    def _reject(self, s, track):
        s.rejected = set(self.policy.reject(s.candidate_view(), track))
        if s.directions:
            s.directions.blocked.add(track['video_id'])

    def seen(self, menu_id):
        """Record the visible menu once, with branch/sequence positions and episode ID.

        #141 can join this provenance to explicit starts and the existing raw player
        event stream. Episode-level credit and counterfactual evaluation belong in
        that shared evaluator: four directions are choices from one serving policy,
        and six generated plays are not six independent positive choices.
        """
        with self.operation:
            s = self.session
            if not s or not s.directions or menu_id != s.directions.menu_id:
                return {"ok": False, "stale": True}
            if menu_id in s.seen_menus:
                return {"ok": True}
            branches = s.directions.public([])
            items = [{**track, "lane": branch["id"], "serving_context": {
                "proposal_id": branch["id"], "slot": branch["slot"],
                "sequence_position": index + 1, "active": branch["active"]}}
                for branch in branches for index, track in enumerate(branch["tracks"])]
            measurement.record_recommendations(self.ctx.store, "radio_directions", items,
                self.ctx.now_fn(), request_id=menu_id, ranker="radio-session",
                provenance={"episode_id": s.episode_id, "menu_id": menu_id,
                    "radio_policy": "saved-playlist-v1",
                    "tuning": s.candidates.tuning,
                    "revision": s.directions.revision, "fingerprint": s.candidates.fingerprint,
                    "history": [t["key"] for t in s.heard],
                    "choice_unit": "radio_session"})
            s.seen_menus.add(menu_id)
            worker = getattr(self.ctx, "enrich_worker", None)
            keys = list(dict.fromkeys(t["key"] for t in self.policy.metadata_tracks(
                        s.candidate_view(), s.history_view(), selected_slot(s))
                        if not t.get("genre") or not t.get("year")))
            if worker and keys:
                worker.prioritize(keys)
            return {"ok": True}

    def _refresh_metadata(self, s):
        now = time.monotonic()
        if not s.seen_menus or now - s.metadata_checked_at < 6:
            return
        s.metadata_checked_at = now
        view, history = s.candidate_view(), s.history_view()
        rejected = self.policy.refresh_metadata(view, history, self.policy.metadata_tracks(view, history, selected_slot(s)))
        if rejected is not None:
            s.metadata_revision += 1
            s.metadata_rejected = set(rejected)
            if rejected - s.directions.blocked:
                s.directions.blocked.update(rejected)
                s.feedback_dirty = True

    def _refresh_candidates(self, s):
        """Upgrade a cold session from prepared pools, without changing its saved queue.

        Runs under the operation lock in the monitor. A pending user choice or
        playlist transaction settles first; playback and the active plan stay put.
        """
        if (s is not self.session or s.stop.is_set() or not s.directions or s.candidates.modes
                or s.initial_tracks or s.pending or s.replacement or s.requested_direction
                or time.monotonic() - s.candidates_checked_at < 6):
            return
        s.candidates_checked_at = time.monotonic()
        store = self.ctx.store
        with store._lock:
            ready = bool(store.settings.get_setting("rec_content_model")
                         and store.rec_surface.get_proposals("mode_bundles"))
        if not ready:
            worker = getattr(self.ctx, "rec_worker", None)
            if worker and not worker.busy and not s.preparation_requested:
                worker.trigger_mode_refresh()
                s.preparation_requested = True
            return
        candidates = RadioCandidates(store, self.ctx.now_fn(), s.candidates.tuning,
                                     now_source=s.candidates)
        if not candidates.modes:
            return
        directions = self.policy.upgrade_directions(s.candidate_view(), s.history_view(), candidates, force=s.feedback_dirty)
        with self.lock:
            if s is not self.session or s.stop.is_set():
                return
            s.candidates, s.directions = candidates, directions
            s.exhausted = False
            s.feedback_dirty = False
            self._persist(s)
        logger.info("Radio upgraded fallback candidates: regions=%d", len(candidates.modes))

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
                store = self.ctx.store
                with (store.priority.background(check_stopping),
                      cancellable_work(lambda: s.stop.is_set() or not self.work.accepting),
                      work_span("radio: current listening"), store.read_snapshot() as snapshot):
                    sample = candidates.sample_now(now, store=snapshot.store)
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

    def _tail_view(self, s):
        now = self.ctx.now_fn()
        s.edits.recent_states = [state for state in s.edits.recent_states if now - state["at"] < 60]
        return TailView(tuple(s.tracks), s.position, s.furthest,
                        frozenset(s.directions.blocked) if s.directions else frozenset(),
                        s.candidates.by_video, tuple(tuple(state["ids"]) for state in s.edits.recent_states),
                        s.skip_requested)

    def _accept_tail(self, s, tail, remote):
        result = self.mutations.recover_tail(self._target(s), tail, self._tail_view(s), remote)
        self._apply_mutation_result(s, result)
        self._persist(s)
        return result.status == 'confirmed'

    def _reject_tail(self, s, tail, track):
        self._reject(s, track)
        tail.clear_wait()
        tail.proposed = self.policy.replenish(s.candidate_view(), s.history_view(), tail.proposed, tail.slot)
        self._persist(s)

    def _apply_tail(self, s, tail):
        """Coordinate policy/playback between confirmed transactions, bounded for Stop."""
        tail.saved = False
        for _ in range(7):
            if s.stop.is_set():
                return False
            result = self.mutations.read_tail(self._target(s), tail, self._tail_view(s))
            self._apply_mutation_result(s, result)
            self._persist(s)
            if result.rejected:
                self._reject_tail(s, tail, result.rejected)
                continue
            if result.tracks is None:
                return False
            remote = list(result.remote)
            ids = tuple(row['videoId'] for row in remote)
            self._browser(s)
            player = self._player(s)
            if player and player.get("video_id") in ids:
                self._observe(s, ids.index(player["video_id"]))
            # An acknowledged write can have raced a song change. Replan the
            # unsent future only after pending readback has settled that write.
            view, history = s.candidate_view(), s.history_view()
            if any(self.policy.genre_mismatch(view, history, t) for t in tail.proposed):
                valid = [t for t in tail.proposed if not self.policy.invalid(view, history, t)]
                tail.proposed = self.policy.replenish(view, history, valid, tail.slot)
                tail.choice_id = None
                self._persist(s)
            decision = tail.plan(remote, self._tail_view(s), player)
            if decision.complete:
                return True
            edit = decision.edit
            if edit is None or s.stop.is_set():
                return False
            result = self.mutations.edit_tail(self._target(s), s.edits, self._tail_view(s), edit, tuple(remote))
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
        self._browser(s)
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
        self._update_directions(s)
        return True

    def select(self, proposal_id, menu_id):
        # Accept the user's choice without waiting on the browser or YouTube.
        # The observer owns playlist writes and finishes any in-flight append first.
        with self.lock:
            s = self.session
            if not self._is_current(s) or not s.directions:
                return {"ok": False, "reason": "Start a Radio Session first."}
            slot, branch = s.directions.find(proposal_id)
            if menu_id != s.directions.menu_id or branch is None:
                return {"ok": False, "stale": True, "reason": "The next songs changed. Choose a direction again."}
            if slot == selected_slot(s):
                return {"ok": True, **self.status()}
            if not branch["tracks"]:
                return {"ok": False, "reason": "No more library songs are available in that direction."}
            s.edits.queue({"slot": slot, "tracks": list(branch["tracks"]),
                           "choice": {"id": branch["id"], "menu_id": menu_id}})
            s.error = None
            self._persist(s)
            return {"ok": True, **self.status()}

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
            metadata = list(s.tracks) + s.heard + [t for b in s.directions.slots if b for t in b["tracks"]]
            if s.replacement:
                metadata += s.replacement.proposed
            candidates.retain(metadata)
            with self.lock:
                selected = selected_slot(s)
                choice = (s.requested_direction["choice"] if s.requested_direction else
                          s.replacement.choice_id if s.replacement else None)
                s.candidates = s.directions.candidates = candidates
                s.directions.blocked.difference_update(s.metadata_rejected - s.rejected)
                s.metadata_rejected.clear()
                s.directions.active = selected
                s.directions.update(s.heard or s.tracks[:1], [], s.transition, force=True)
                branch = s.directions.slots[selected]
                s.edits.queue({"slot": selected, "tracks": list(branch["tracks"]),
                               "choice": choice if branch["tracks"] else None})
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
            candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn())
            seeds = []
            if playlist_id is not None:
                source = self.ctx.store.playlists.get_playlist(playlist_id)
                if source is None:
                    return {"ok": False, "reason": "That playlist no longer exists. Choose another playlist."}
                seen = set()
                for track in self.ctx.store.charts.playlist_tracks_detail(playlist_id):
                    video = track.get("video_id")
                    if video and video not in seen and track.get("available") != 0:
                        seen.add(video)
                        seeds.append({**track, "key": track["identity_key"]})
                if not seeds:
                    return {"ok": False, "reason": "This playlist has no playable songs. Choose another playlist."}
                candidates.retain(seeds)
            seed = (seeds[0] if seeds else candidates.by_video.get(seed_video_id) if seed_video_id
                    else next(iter(candidates.seed_offer(1)), None))
            if seed is None and seed_video_id:
                # Offers outlive a prepared-pool rebuild. Resolve the exact upload
                # from the catalog instead of treating pool membership as availability.
                with self.ctx.store._lock:
                    row = self.ctx.store.conn.execute(
                        "SELECT identity_key AS key, video_id, title, artist, album, thumbnail, "
                        "duration_s AS duration, genre, mb_year AS year, available "
                        "FROM catalog_tracks WHERE video_id=? LIMIT 1", (seed_video_id,)).fetchone()
                if row:
                    track = dict(row)
                    blocked = self.ctx.store.rec_model.disliked_identity_keys()
                    muted = {a.casefold() for a in self.ctx.store.rec_model.muted_artists()}
                    if (track["key"] not in blocked and track.get("available") != 0
                            and (track.get("artist") or "").casefold() not in muted):
                        seed = track
                        candidates.retain([seed])
            if seed is None:
                # An expired upload should still lead somewhere useful. Prefer
                # another current offer and let the usual playlist writer filter it.
                seed = next(iter(candidates.seed_offer(1)), None)
            if seed is None:
                self.offer = None
                return {"ok": False, "refresh_seeds": True, **self.status()}
            title = versioned_title(self.ctx.store, f"Radio Session · {day.isoformat()}")
            with self.lock:
                if self.session:
                    self.session.stop.set()
                s = self.session = Session(client, candidates, identity, title)
                s.started_at = self.ctx.now_fn()
                s.selected = list(seeds or [seed])
                s.choices[seed["video_id"]] = {"id": uuid4().hex,
                    "menu_id": f"playlist:{playlist_id}" if playlist_id is not None else menu_id or self.offer_id or "direct"}
            try:
                seeds = list(s.selected)
                for _ in range(max(0, INITIAL_TRACKS - len(seeds))):
                    if s.stop.is_set():
                        return {"ok": False, "reason": "Stopped during start."}
                    pick = self.policy.pick(s.candidate_view())
                    if pick is None and playlist_id is not None:
                        break
                    if pick is None:
                        raise ValueError("Fewer than three playable songs are available. Sync your YouTube Music library first.")
                    seeds.append(pick)
                    s.selected.append(pick)
                if s.stop.is_set():
                    return {"ok": False, "reason": "Stopped during start."}
                result = executor.create_generated_playlist(
                    self.ctx.store, s.title, seeds, client, self.ctx.now_fn(), identity)
                with self.lock:
                    s.playlist = result["new_ytm"]
                    s.local_id = result["pid"]
                # The writer can omit unavailable or over-length tracks. Wait for
                # precisely the accepted snapshot, rather than the requested rows.
                seeds = [{**track, "key": track["identity_key"]}
                    for track in self.ctx.store.charts.playlist_tracks_detail(s.local_id)]
                if not seeds:
                    raise ValueError("No playable songs could be added to radio. Choose another playlist or song.")
                candidates.retain(seeds)
                s.selected = list(seeds)
                choice = s.choices.pop(seed["video_id"])
                seed = seeds[0]
                s.choices[seed["video_id"]] = choice
                # Retain all session playlist IDs as provenance: automatic listening
                # must not become an independent positive taste vote on each song.
                settings = self.ctx.store.settings
                known_ids = json.loads(settings.get_setting("radio_session_playlist_ids") or "[]")
                settings.set_setting("radio_session_playlist_ids", json.dumps(list(dict.fromkeys(known_ids + [s.playlist]))))
                with self.lock:
                    s.initial_tracks = seeds
                    s.preparation_deadline = time.monotonic() + PREPARATION_TIMEOUT
                    s.directions = RadioDirections(candidates, seed)
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
        """Wait for the existing playlist to settle, without recreating it."""
        seeds = s.initial_tracks
        now = time.monotonic()
        if s.preparation_deadline and now >= s.preparation_deadline:
            raise PlaylistChanged("Radio playlist preparation timed out.")
        expected = [t["video_id"] for t in seeds]
        try:
            received = [t["videoId"] for t in self._remote_tracks(s, expected=expected)]
        except Exception:
            s.preparation_snapshot = None
            raise
        if received != expected:
            report = ("preparing", tuple(received))
            if report != s.observer_report:
                logger.info("Radio playlist preparation pending: playlist=%s expected=%s received=%s",
                            s.playlist, expected, received)
                s.observer_report = report
            # YouTube can acknowledge a batch while silently omitting a song.
            # Accept only a complete, stable, ordered subset of our own writes;
            # transient pages, reorders and unrelated edits never qualify.
            accepted = set(received)
            if [video for video in expected if video in accepted] != received:
                s.preparation_snapshot = None
                raise PlaylistPending()
            if s.preparation_snapshot != tuple(received):
                s.preparation_snapshot = tuple(received)
                s.preparation_seen_at = now
            if now - s.preparation_seen_at < PREPARATION_SETTLE:
                raise PlaylistPending()
            if not received:
                raise PlaylistChanged("YouTube Music did not save any playable songs. Choose another song.")
            omitted = [track for track in seeds if track["video_id"] not in accepted]
            seeds = [track for track in seeds if track["video_id"] in accepted]
            choice = s.choices.pop(s.initial_tracks[0]["video_id"], None)
            if choice:
                s.choices[seeds[0]["video_id"]] = choice
            s.directions = RadioDirections(s.candidates, seeds[0])
            for track in omitted:
                self._reject(s, track)
        self._publish(s, seeds)
        with self.lock:
            s.initial_tracks = []
            s.preparation_deadline = 0
            s.preparation_snapshot = None
            if not s.stopped_by_user:
                s.move(Phase.AWAITING_PLAYBACK)
            s.launch_deadline = time.monotonic() + 15
            s.error = None
            self._update_directions(s)
            self._persist(s)
            if not s.stop.is_set():
                self.playback.navigate(s.playlist, seeds[0]["video_id"])

    def _recover(self, s):
        """Reconcile our in-flight write without discarding unrelated saved edits."""
        remote = self._remote_tracks(s)
        actual = tuple(t["videoId"] for t in remote)
        allowed = {tuple(t["video_id"] for t in s.tracks)}
        if s.pending:
            allowed.add((*next(iter(allowed)), s.pending["video_id"]))
            receipt = (s.edits.append.write.receipt if s.edits.append.write else None) or {}
            if receipt.get("videoId"):
                allowed.add(tuple(t["video_id"] for t in s.tracks) + (receipt["videoId"],))
        if s.replacement:
            try:
                if not self._accept_tail(s, s.replacement, remote):
                    return False
            except PlaylistChanged:
                return False
            allowed.update(s.replacement.allowed)
        if actual not in allowed and not (s.edits.append and self.mutations.append_substitution(
                s.edits.append, s.tracks, remote)):
            return False
        s.move(s.running_phase())
        s.error = None
        s.edits.conflict = None
        logger.info("Radio saved-playlist readback recovered: playlist=%s", s.playlist)
        return True

    def _wait_for_skip(self, s, video_id, deadline):
        while time.monotonic() < deadline and not s.stop.wait(.25):
            try:
                self._browser(s)
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
                        self._browser(s)
                        player = self._player(s)
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
                            self._browser(s)
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
                            self._browser(s)
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
                            self._browser(s)
                            if s.halted:
                                # A conflict pauses mutations, not the radio UI.
                                # Resume only after readback returns to a state
                                # owned by this transaction; never overwrite an edit.
                                if not self._recover(s):
                                    continue
                            self._refresh_metadata(s)
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
        if s.stop.is_set() or self._player(s) is None:
            return
        # Resolve an uncertain append before a switch or feedback replacement.
        if s.pending is not None:
            if not self._append(s):
                return
        # A resumed transaction may already have added the playing song remotely
        # while its last local mirror failed. Reconcile it before looking up position.
        if s.replacement:
            changed = self._feedback(s)
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
        player = self._player(s)
        position = next((i for i, t in enumerate(s.tracks)
                         if player and t["video_id"] == player.get("video_id")), None)
        if position is None:
            return
        self._observe(s, position)
        changed = self._feedback(s)
        if position < s.furthest:
            self._update_directions(s, force=changed)
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
        self._update_directions(s, force=changed)
        # A bounded pass keeps Stop responsive even when someone skips rapidly.
        for _ in range(LOOKAHEAD + 1):
            player = self._player(s)
            if s.stop.is_set() or player is None:
                return
            position = next((i for i, t in enumerate(s.tracks)
                             if t["video_id"] == player.get("video_id")), None)
            if position is None:
                return
            self._observe(s, position)
            self._update_directions(s)
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
        result = self.mutations.append(self._target(s), s.edits, self._tail_view(s))
        self._apply_mutation_result(s, result)
        if result.status == 'confirmed':
            with self.lock:
                s.edits.finish(operation)
                s.exhausted = False
                s.error = None
                s.refresh_needed = True
            self._persist(s)
            self._browser(s)
            return True
        if result.status in {'rejected', 'discarded'}:
            if result.rejected:
                self._reject(s, result.rejected)
            s.edits.finish(operation)
            s.selected = list(s.tracks)
            s.error = None
            self._persist(s)
            return True
        return False
