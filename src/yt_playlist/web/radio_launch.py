"""Seed resolution, initial playlist creation and readback readiness.

Launch uses explicit client/session inputs. The coordinator chooses the current
session and controls navigation; this owner never calls back into that coordinator.
"""
import json
import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
from uuid import uuid4

from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.core.activity import WorkClosing
from yt_playlist.library import playlist_generated
from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, versioned_title
from yt_playlist.library.playlist_snapshot import http_status
from yt_playlist.library.radio_launch_publication import RadioLaunchPublication
from yt_playlist.library.undo_conflicts import playlist_state
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec import choice_evidence
from yt_playlist.rec.radio_startup import eligible_seeds, prepared_launch_tracks
from yt_playlist.rec import radio_rotation, rec_params
from yt_playlist.rec.radio_restored import RestoredCandidates
from yt_playlist.web.radio_persistence import PREPARATION_TIMEOUT, restore
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending, readback_durations
from yt_playlist.web.radio_mutations import RadioMutations
from yt_playlist.web.radio_projection import saved_view
from yt_playlist.web.radio_state import Phase, Session

logger = logging.getLogger(__name__)
INITIAL_TRACKS = 3
PREPARATION_SETTLE = 4


class SeededCreateUncertain(Exception):
    """The provider may have created the seeded playlist without confirming it."""


def seeded_description(episode):
    return f'{GENERATED_DESCRIPTION}. Radio session {episode}'


@dataclass(frozen=True)
class PreparedStart:
    tracks: tuple[dict, ...]
    omitted: tuple[dict, ...] = ()


class RadioLaunch:
    def __init__(self, store, playlists, persistence, policy, lock, now_fn):
        self.store, self.playlists, self.persistence, self.policy = store, playlists, persistence, policy
        self.lock, self.now_fn = lock, now_fn

    def retain_provenance(self):
        """Keep source identity once writes resume, without replaying activity or a checkpoint."""
        with self.store._lock:
            if getattr(self.store, 'activity_paused', False):
                return
            pending = self.store.transient_radio_playlists.copy()
            if not pending:
                return
            for playlist in pending:
                self.store.playlists.mark_generated_playlist(playlist)
            self.store.radio_checkpoints.remember_playlists(pending)
            self.store.transient_radio_playlists.difference_update(pending)

    def checkpoint_view(self, playing, *, reader=None):
        """A saved checkpoint alone cannot establish current native playback."""
        try:
            saved = (self.persistence.load() if reader is None else self.persistence.view(reader)) or {}
        except (ValueError, KeyError, TypeError):
            saved = {}
        active = bool(saved.get('playlist') and playing.get('playlist_ytm_id') == saved['playlist']
                      and not saved.get('stopped'))
        return saved_view(saved, active=active)

    def resolve(self, candidates, client, identity, day, *, seed_video_id=None,
                playlist_id=None, menu_id=None, source_queue=None):
        seeds = []
        if playlist_id is not None:
            seeds = self.store.radio_catalog.playlist_queue(playlist_id) if source_queue is None else source_queue
            if seeds is None:
                raise ValueError("That playlist no longer exists. Choose another playlist.")
            if not seeds:
                raise ValueError("This playlist has no playable songs. Choose another playlist.")
            candidates.retain(seeds)
        seed = (seeds[0] if seeds else candidates.by_video.get(seed_video_id) if seed_video_id
                else next(iter(candidates.seed_offer(1)), None))
        if seed is None and seed_video_id:
            # A previously offered upload may outlive the prepared pool that contained it.
            row = self.store.radio_catalog.details([seed_video_id]).get(seed_video_id)
            if row:
                seed = dict(row)
        if seed is not None and not seeds:
            seed = next(iter(eligible_seeds(self.store, [seed], self.now_fn())), None)
            if seed is not None:
                candidates.retain([seed])
        if seed is None:
            seed = next(iter(eligible_seeds(self.store, candidates.seed_offer(1), self.now_fn())), None)
        if seed is None:
            return None
        return self._session(candidates, client, identity, day, seeds or [seed], playlist_id, menu_id,
                             seed_video_id=seed_video_id)

    def resolve_known(self, reader, client, identity, day, *, source_queue, playlist_id, seed_video_id,
                      menu_id, offer, factory):
        seeds = list(source_queue or ())
        if playlist_id is None:
            offered = next((track for track in offer or () if track['video_id'] == seed_video_id), None)
            selected = reader.radio_catalog.launch_seed(seed_video_id)
            if selected is None:
                return None
            resolved = eligible_seeds(reader, [offered or {**selected, 'video_id': seed_video_id}], self.now_fn(),
                                      current={seed_video_id: selected})
            if not resolved:
                raise ValueError('Choose a currently playable On Air song.')
            candidates = RestoredCandidates(self.store, self.now_fn, {}, factory, initial_queue=True)
            candidates.retain(resolved)
            s = self._session(candidates, client, identity, day, resolved, playlist_id, menu_id,
                              reader=reader, original_seed=seed_video_id)
            s.seeded_launch = True
            return s
        if not seeds:
            return None
        maximum = rec_params.max_track_duration_s(reader)
        preferred = reader.tracks.preferred_playback_uploads(t['video_id'] for t in seeds)
        resolved = [{**track, **preferred.get(track['video_id'], {})} for track in seeds]
        facts = reader.radio_catalog.rotation_facts(track['key'] for track in resolved)
        for track in resolved:
            track.update(radio_rotation.metadata(facts.get(track['key'])))
            radio_rotation.issue(track)
        if len(resolved) < INITIAL_TRACKS:
            resolved.extend(prepared_launch_tracks(reader, resolved, INITIAL_TRACKS - len(resolved),
                                                    self.now_fn(), maximum))
        if len(resolved) < INITIAL_TRACKS:
            return None
        candidates = RestoredCandidates(self.store, self.now_fn, {}, factory, initial_queue=True)
        candidates.retain(resolved)
        s = self._session(candidates, client, identity, day, resolved, playlist_id, menu_id,
                          reader=reader, original_seed=seeds[0]['video_id'])
        s.initial_queue_maximum = maximum
        return s

    def _session(self, candidates, client, identity, day, seeds, playlist_id, menu_id, *,
                 reader=None, seed_video_id=None, original_seed=None):
        seed = seeds[0]
        for track in seeds:
            radio_rotation.issue(track)
        prefix = f"Radio Session · {day.isoformat()}"
        title = (f"{prefix} #{reader.playlists.count_title_prefix(prefix) + 1}" if reader is not None
                 else versioned_title(self.store, prefix))
        s = Session(client, candidates, identity, title)
        s.started_at, s.selected = self.now_fn(), list(seeds)
        s.choices[seed["video_id"]] = {"id": uuid4().hex,
            "option_id": original_seed or seed_video_id or seed["video_id"],
            "menu_id": f"playlist:{playlist_id}" if playlist_id is not None else menu_id or "direct"}
        return s

    def record_seed_choice(self, menu_id, option_id, episode_id, requested_at):
        choice_evidence.safe_respond(self.store, menu_id, 'choice', requested_at,
            option_id=option_id, details={'episode_id': episode_id})

    def create_seeded(self, s):
        seed = s.selected[0]
        description = seeded_description(s.episode_id) if s.launch_id else GENERATED_DESCRIPTION
        with work_span('radio: provider create with seed', announce=True):
            try:
                playlist = s.client.create_playlist(s.title, description, video_ids=[seed['video_id']])
            except Exception as exc:
                if (not s.launch_id or getattr(exc, 'outcome', None) == 'not-dispatched'
                        or http_status(exc) in (400, 401, 403)):
                    raise
                raise SeededCreateUncertain(str(exc)) from exc
        if not isinstance(playlist, str) or not playlist:
            error = SeededCreateUncertain if s.launch_id else PlaylistChanged
            raise error('YouTube Music did not confirm playlist creation.')
        return playlist

    def find_seeded(self, s):
        """Adopt only a playlist whose exact attempt and seed the provider confirms."""
        seed = s.selected[0]['video_id']
        expected = seeded_description(s.episode_id)
        listing = s.client.get_library_playlists(limit=None)
        candidates = dict.fromkeys(row['playlistId'] for row in listing
            if row.get('title') == s.title and row.get('playlistId'))
        matches = []
        for playlist in candidates:
            detail = self.playlists.read_snapshot(s.client, playlist)
            if (detail.get('description') == expected and detail.get('title') == s.title
                    and [t['videoId'] for t in detail['tracks']] == [seed]
                    and detail['tracks'][0].get('isAvailable') is not False):
                matches.append(playlist)
        return matches[0] if len(matches) == 1 else None

    def accept_seeded(self, s, playlist):
        seed = s.selected[0]
        with self.lock:
            s.playlist = playlist
            s.accept_tracks([seed], self.now_fn())
            s.directions = RadioDirections(s.candidates, seed)
            s.preparation_deadline = time.monotonic() + PREPARATION_TIMEOUT
            if not s.stopped_by_user:
                s.move(Phase.AWAITING_PLAYBACK)
                s.launch_deadline = time.monotonic() + 15

    def restore_seeded(self, recovery, client, factory):
        candidates = RestoredCandidates(self.store, self.now_fn, {}, factory, initial_queue=True)
        seed = recovery['seed']
        candidates.retain([seed])
        return Session(client, candidates, recovery['identity'], recovery['title'],
                       selected=[seed], choices=recovery['choices'], seeded_launch=True,
                       episode_id=recovery['episode_id'], started_at=recovery['started_at'],
                       launch_id=recovery['launch_id'])

    def retain_seeded(self, playlist):
        with self.store._lock:
            self.store.transient_radio_playlists.add(playlist)
        try:
            self.retain_provenance()
        except Exception:
            logger.warning('Could not retain Radio playlist provenance: %s', playlist, exc_info=True)

    def publish_seeded(self, s):
        """Turn the observed starting queue into the saved playlist and Undo."""
        expected = [t['video_id'] for t in s.tracks]
        snapshot = s.initial_readback or self._read_initial_snapshot(s.client, s.playlist, expected)
        if (not snapshot or snapshot.get('_radio_count_adjusted')
                or [t['videoId'] for t in snapshot['tracks']] != expected
                or not all(t.get('isAvailable') is not False for t in snapshot['tracks'])):
            raise PlaylistPending()
        if (snapshot.get('title') not in (None, s.title)
                or snapshot.get('description') not in (None, GENERATED_DESCRIPTION, seeded_description(s.episode_id))):
            raise PlaylistChanged('The playlist was changed elsewhere.')
        with self.lock:
            s.accept_tracks(readback_durations(s.tracks, snapshot['tracks']), self.now_fn())
            if not getattr(self.store, 'activity_paused', False):
                s.initial_publication = RadioLaunchPublication.for_seeded(
                    s.title, s.identity, s.playlist, playlist_state(snapshot))
                s.save_effects = tuple(t['key'] for t in s.tracks)

    def create(self, s, *, from_playlist=False):
        seeds = list(s.selected)
        seed = seeds[0]
        prepared = getattr(s.candidates, 'prepared', None)
        with work_span('radio: initial queue', announce=True), (
                prepared.planning_read() if prepared is not None and len(seeds) < INITIAL_TRACKS else nullcontext()):
            if s.initial_queue_maximum is None:
                seeds = self._resolve_playback(s, seeds)
            s.selected = list(seeds)
            if len(seeds) < INITIAL_TRACKS and not s.stop.is_set() and (
                    startup := getattr(s.candidates, 'startup_tracks', None)):
                picks = startup(seeds, INITIAL_TRACKS - len(seeds))
                seeds.extend(picks or ())
                s.selected.extend(picks or ())
            for _ in range(max(0, INITIAL_TRACKS - len(seeds))):
                if s.stop.is_set():
                    return False
                pick = self.policy.next_track(s.candidate_view())
                if pick is None and from_playlist:
                    break
                if pick is None:
                    raise ValueError("Fewer than three playable songs are available. Sync your YouTube Music library first.")
                actual, = self._resolve_playback(s, [pick])
                seeds.append(actual)
                s.selected.append(actual)
        if s.stop.is_set():
            return False
        from yt_playlist.library.generated_composition import commands
        options = ({'max_duration_s': s.initial_queue_maximum, 'defer_radio_publication': True}
                   if s.initial_queue_maximum is not None else {})
        readback = (partial(self.initial_readback, retry=lambda: not s.stop.is_set())
                    if options else self.initial_readback)
        with work_span('radio: initial saved playlist write', announce=True):
            result = commands(self.store, self.now_fn()).create(s.title, seeds,
                client=s.client, account_id=s.identity, radio_readback=readback, **options)
        logger.info("Radio initial write: playlist=%s requested=%s acknowledged=%s",
                    result["new_ytm"], [t['video_id'] for t in seeds], result.get("added_handles", {}))
        allocations = {track['key']: track.get('_radio_rotation_slot') for track in seeds}
        seeds = result['tracks']
        if not seeds:
            with self.lock:
                s.playlist, s.local_id = result["new_ytm"], result["pid"]
            raise ValueError("No playable songs could be added to radio. Choose another playlist or song.")
        s.candidates.retain(seeds)
        for track in seeds:
            # A catalog write can resolve a video to its canonical recording.
            slot = allocations.get(track['key'])
            if slot in radio_rotation.KINDS:
                track['_radio_rotation_slot'] = slot
            else:
                radio_rotation.issue(track)
        s.selected = list(seeds)
        choice = s.choices.pop(seed["video_id"])
        s.choices[seeds[0]["video_id"]] = choice
        with self.lock:
            s.playlist, s.local_id = result["new_ytm"], result["pid"]
            s.initial_handles = result.get("added_handles", {})
            s.initial_readback = result.get('initial_snapshot')
            s.initial_publication = result.get('launch_publication')
            if s.initial_publication is not None:
                seeds = list(readback_durations(seeds, s.initial_readback['tracks']))
                s.save_effects = tuple(track['key'] for track in seeds)
            s.initial_tracks = seeds
            s.preparation_deadline = time.monotonic() + PREPARATION_TIMEOUT
            s.directions = RadioDirections(s.candidates, seeds[0])
        if s.local_id is None and s.initial_publication is None:
            with self.store._lock:
                self.store.transient_radio_playlists.add(s.playlist)
                # Record the generated playlist as soon as writes are available.
                self.retain_provenance()
        return True

    def _resolve_playback(self, s, seeds):
        prepared = getattr(s.candidates, 'prepared', None)
        reader = getattr(prepared, 'current_reader', None) or self.store
        preferred = reader.tracks.preferred_playback_uploads(t['video_id'] for t in seeds)
        resolved = [{**track, **preferred.get(track['video_id'], {})} for track in seeds]
        changed = [actual for requested, actual in zip(seeds, resolved) if actual['key'] != requested['key']]
        if changed:
            s.candidates.retain(changed)
            for track in changed:
                radio_rotation.issue(track)
        return resolved

    def initial_readback(self, client, playlist, expected, *, retry=None):
        snapshot = self._read_initial_snapshot(client, playlist, expected)
        if retry is not None and (snapshot is None or not snapshot['tracks']) and retry():
            # New destinations can briefly be empty after acknowledged adds.
            # Give that read one immediate retry before committing to recovery.
            with work_span('radio: immediate empty readback retry', announce=True):
                refreshed = self._read_initial_snapshot(client, playlist, expected)
            if refreshed is not None:
                return refreshed
        return snapshot

    def _read_initial_snapshot(self, client, playlist, expected):
        try:
            with work_span('radio: provider readback', announce=True):
                return self.playlists.read_snapshot(client, playlist, expected=expected)
        except WorkClosing:
            raise
        except Exception as exc:
            logger.debug('Radio initial playlist readback deferred: %s', type(exc).__name__)
            return None

    def confirm(self, s):
        seeds, now = s.initial_tracks, time.monotonic()
        if s.preparation_deadline and now >= s.preparation_deadline:
            raise PlaylistChanged("Radio playlist preparation timed out.")
        expected = [t["video_id"] for t in seeds]
        try:
            with work_span('radio: initial saved playlist readback', announce=True):
                remote = (s.initial_readback['tracks'] if s.initial_readback is not None
                          else self.playlists.read(s.client, s.playlist, expected=expected))
            received = [t["videoId"] for t in remote]
        except WorkClosing:
            raise
        except Exception as exc:
            s.preparation_snapshot = None
            raise PlaylistPending() from exc
        if received == expected and all(row.get('isAvailable') is not False for row in remote):
            return PreparedStart(readback_durations(seeds, remote))
        s.initial_readback = None
        report = ("preparing", tuple(received))
        if report != s.observer_report:
            logger.info("Radio playlist preparation pending: playlist=%s expected=%s received=%s",
                        s.playlist, expected, received)
            s.observer_report = report
        # Music's playback ID can differ from the saved upload ID. The write's
        # occurrence handle proves the correspondence even when credits differ.
        matched, position = [], -1
        for row in remote:
            candidates = [(i, track) for i, track in enumerate(seeds) if
                track['video_id'] in (row['videoId'], row.get('playlistVideoId')) or
                (row.get('setVideoId') and s.initial_handles.get(track['video_id']) == row['setVideoId'])]
            if len(candidates) != 1 or candidates[0][0] <= position:
                s.preparation_snapshot = None
                raise PlaylistPending()
            position, track = candidates[0]
            matched.append(track)
        accepted = {track['video_id'] for track in matched}
        if len(accepted) != len(received):
            s.preparation_snapshot = None
            raise PlaylistPending()
        if s.preparation_snapshot != tuple(received):
            s.preparation_snapshot, s.preparation_seen_at = tuple(received), now
        if now - s.preparation_seen_at < PREPARATION_SETTLE:
            raise PlaylistPending()
        if not received:
            raise PlaylistChanged("YouTube Music did not save any playable songs. Choose another song.")
        confirmed = []
        substitutions = RadioMutations(self.playlists, self.persistence, self.store.tracks, self.now_fn,
                                       paused=lambda: getattr(self.store, 'activity_paused', False),
                                       rotation_facts=self.store.radio_catalog.rotation_facts)
        for track, row in zip(matched, remote):
            if row.get('isAvailable') is False:
                accepted.discard(track['video_id'])
            elif track['video_id'] != row['videoId']:
                substitution = substitutions.substitute(track, row, s.candidates.by_video,
                                                       playlist=s.playlist)
                actual = substitution.accepted
                s.candidates.retain([actual])
                if actual['key'] != track['key']:
                    radio_rotation.issue(actual)
                choice = s.choices.pop(track['video_id'], None)
                if choice:
                    s.choices[actual['video_id']] = choice
                s.rejected.add(track['video_id'])
                confirmed.append(actual)
            else:
                confirmed.append(track)
        if not confirmed:
            raise PlaylistChanged("YouTube Music did not save any playable songs. Choose another song.")
        return PreparedStart(readback_durations(confirmed, remote),
                             tuple(t for t in seeds if t["video_id"] not in accepted))

    def resume(self, state, playing, clients, candidates_factory):
        if not state or state.get("stopped"):
            return None
        identity = next((key for key in clients if str(key) == str(state["identity"])), None)
        if (identity is None or not state.get("playlist")
                or state["playlist"] != playing.get("playlist_ytm_id")
                or not (state.get("tracks") or state.get("initial_tracks"))):
            return None
        # These songs and branches have already been published. Reattachment
        # needs their checkpoint and live ownership, not another ranking pass.
        # The monitor loads current candidates when future planning needs them.
        candidates = RestoredCandidates(self.store, self.now_fn, state, candidates_factory)
        return restore(state, clients[identity], identity, candidates)

    def accept(self, s, prepared: PreparedStart):
        """Publish the confirmed initial queue before the coordinator navigates."""
        seeds = list(prepared.tracks)
        if prepared.omitted or seeds[0]['video_id'] != s.initial_tracks[0]['video_id']:
            choice = s.choices.pop(s.initial_tracks[0]['video_id'], None)
            if choice:
                s.choices[seeds[0]['video_id']] = choice
            s.directions = RadioDirections(s.candidates, seeds[0])
            for track in prepared.omitted:
                s.rejected = set(self.policy.reject(s.candidate_view(), track))
                s.directions.blocked.add(track['video_id'])
        s.directions.blocked.update(s.rejected)
        if seeds != s.tracks:
            learned_duration = any(track.get('duration') and not initial.get('duration')
                                   for track, initial in zip(seeds, s.initial_tracks))
            if s.initial_publication is None and (s.initial_readback is None or learned_duration):
                self.playlists.publish(s.local_id, s.title, s.tracks, seeds, self.now_fn())
            s.accept_tracks(seeds, self.now_fn())
        s.initial_tracks = []
        s.initial_handles = {}
        s.initial_readback = None
        s.preparation_deadline = 0
        s.preparation_snapshot = None
        if not s.stopped_by_user:
            s.move(Phase.AWAITING_PLAYBACK)
        s.launch_deadline = time.monotonic() + 15
        s.error = None

    def adopt(self, playing, clients, candidates_factory):
        """Recognize an older already playing session without creating or navigating."""
        playlist = playing.get("playlist_ytm_id")
        if not playlist or playlist not in self.store.playlists.radio_playlist_ids():
            return None
        local = next((p for p in self.store.playlists.get_playlists() if p.provider_playlist_id == playlist), None)
        if local is None or local.account_id not in clients:
            return None
        candidates = candidates_factory(self.store, self.now_fn())
        candidates.retain([{**t, "key": t["identity_key"]}
                           for t in self.store.charts.playlist_tracks_detail(local.id)])
        ids = [t["videoId"] for t in self.playlists.read(clients[local.account_id], playlist)]
        if not ids or any(video not in candidates.by_video for video in ids):
            return None
        s = Session(clients[local.account_id], candidates, local.account_id, local.title)
        s.playlist, s.local_id, s.tracks = playlist, local.id, [candidates.by_video[video] for video in ids]
        s.selected = list(s.tracks)
        s.position = next((i for i, t in enumerate(s.tracks) if t["video_id"] == playing.get("video_id")), -1)
        s.furthest = s.position
        s.move(Phase.AWAITING_PLAYBACK)
        s.playback_confirmed, s.started_at = True, self.now_fn()
        s.settle()
        s.directions = RadioDirections(candidates, s.tracks[0])
        return s
