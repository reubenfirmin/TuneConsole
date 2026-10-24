"""Seed resolution, initial playlist creation and readback readiness.

Launch uses explicit client/session inputs. The coordinator chooses the current
session and controls navigation; this owner never calls back into that coordinator.
"""
import json
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

from yt_playlist.library import playlist_generated
from yt_playlist.library.playlist_metadata import versioned_title
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.web.radio_persistence import PREPARATION_TIMEOUT, restore
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending, readback_durations
from yt_playlist.web.radio_mutations import RadioMutations
from yt_playlist.web.radio_projection import saved_view
from yt_playlist.web.radio_state import Phase, Session

logger = logging.getLogger(__name__)
INITIAL_TRACKS = 3
PREPARATION_SETTLE = 4


@dataclass(frozen=True)
class PreparedStart:
    tracks: tuple[dict, ...]
    omitted: tuple[dict, ...] = ()


class RadioLaunch:
    def __init__(self, store, playlists, persistence, policy, lock, now_fn):
        self.store, self.playlists, self.persistence, self.policy = store, playlists, persistence, policy
        self.lock, self.now_fn = lock, now_fn

    def checkpoint_view(self, playing):
        """A saved checkpoint alone cannot establish current native playback."""
        try:
            saved = self.persistence.load() or {}
        except (ValueError, KeyError, TypeError):
            saved = {}
        active = bool(saved.get('playlist') and playing.get('playlist_ytm_id') == saved['playlist']
                      and not saved.get('stopped'))
        return {**saved_view(saved, active=active), 'resolving': active}

    def resolve(self, candidates, client, identity, day, *, seed_video_id=None,
                playlist_id=None, menu_id=None):
        seeds = []
        if playlist_id is not None:
            if self.store.playlists.get_playlist(playlist_id) is None:
                raise ValueError("That playlist no longer exists. Choose another playlist.")
            seen = set()
            for track in self.store.charts.playlist_tracks_detail(playlist_id):
                video = track.get("video_id")
                if video and video not in seen and track.get("available") != 0:
                    seen.add(video)
                    seeds.append({**track, "key": track["identity_key"]})
            if not seeds:
                raise ValueError("This playlist has no playable songs. Choose another playlist.")
            candidates.retain(seeds)
        seed = (seeds[0] if seeds else candidates.by_video.get(seed_video_id) if seed_video_id
                else next(iter(candidates.seed_offer(1)), None))
        if seed is None and seed_video_id:
            # A previously offered upload may outlive the prepared pool that contained it.
            with self.store._lock:
                row = self.store.conn.execute(
                    "SELECT identity_key AS key, video_id, title, artist, album, thumbnail, "
                    "duration_s AS duration, genre, mb_year AS year, available "
                    "FROM catalog_tracks WHERE video_id=? LIMIT 1", (seed_video_id,)).fetchone()
            if row:
                track = dict(row)
                blocked = self.store.rec_model.disliked_identity_keys()
                muted = {a.casefold() for a in self.store.rec_model.muted_artists()}
                if (track["key"] not in blocked and track.get("available") != 0
                        and (track.get("artist") or "").casefold() not in muted):
                    seed = track
                    candidates.retain([seed])
        if seed is None:
            seed = next(iter(candidates.seed_offer(1)), None)
        if seed is None:
            return None
        s = Session(client, candidates, identity,
                    versioned_title(self.store, f"Radio Session · {day.isoformat()}"))
        s.started_at, s.selected = self.now_fn(), list(seeds or [seed])
        s.choices[seed["video_id"]] = {"id": uuid4().hex,
            "option_id": seed["video_id"],
            "menu_id": f"playlist:{playlist_id}" if playlist_id is not None else menu_id or "direct"}
        if seed_video_id and menu_id:
            from yt_playlist.rec import choice_evidence
            choice_evidence.safe_respond(self.store, menu_id, "choice", self.now_fn(),
                                        option_id=seed["video_id"], details={"episode_id": s.episode_id})
        return s

    def create(self, s, *, from_playlist=False):
        seeds = list(s.selected)
        seed = seeds[0]
        for _ in range(max(0, INITIAL_TRACKS - len(seeds))):
            if s.stop.is_set():
                return False
            pick = self.policy.pick(s.candidate_view())
            if pick is None and from_playlist:
                break
            if pick is None:
                raise ValueError("Fewer than three playable songs are available. Sync your YouTube Music library first.")
            seeds.append(pick)
            s.selected.append(pick)
        if s.stop.is_set():
            return False
        alternates = self.store.tracks.playback_alternates(s.identity, [t['video_id'] for t in seeds])
        seeds = [{**track, **alternates.get(track['video_id'], {})} for track in seeds]
        result = playlist_generated.create_generated_playlist(
            self.store, s.title, seeds, s.client, self.now_fn(), s.identity)
        with self.lock:
            s.playlist, s.local_id = result["new_ytm"], result["pid"]
            s.initial_handles = result.get("added_handles", {})
        logger.info("Radio initial write: playlist=%s requested=%s acknowledged=%s",
                    s.playlist, [t['video_id'] for t in seeds], s.initial_handles)
        seeds = [{**track, "key": track["identity_key"]}
                 for track in self.store.charts.playlist_tracks_detail(s.local_id)]
        if not seeds:
            raise ValueError("No playable songs could be added to radio. Choose another playlist or song.")
        s.candidates.retain(seeds)
        s.selected = list(seeds)
        choice = s.choices.pop(seed["video_id"])
        s.choices[seeds[0]["video_id"]] = choice
        settings = self.store.settings
        known = json.loads(settings.get_setting("radio_session_playlist_ids") or "[]")
        settings.set_setting("radio_session_playlist_ids", json.dumps(list(dict.fromkeys(known + [s.playlist]))))
        with self.lock:
            s.initial_tracks = seeds
            s.preparation_deadline = time.monotonic() + PREPARATION_TIMEOUT
            s.directions = RadioDirections(s.candidates, seeds[0])
        return True

    def confirm(self, s):
        seeds, now = s.initial_tracks, time.monotonic()
        if s.preparation_deadline and now >= s.preparation_deadline:
            raise PlaylistChanged("Radio playlist preparation timed out.")
        expected = [t["video_id"] for t in seeds]
        try:
            remote = self.playlists.read(s.client, s.playlist, expected=expected)
            received = [t["videoId"] for t in remote]
        except Exception:
            s.preparation_snapshot = None
            raise
        if received == expected and all(row.get('isAvailable') is not False for row in remote):
            return PreparedStart(readback_durations(seeds, remote))
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
        substitutions = RadioMutations(self.playlists, self.persistence, self.store.tracks, self.now_fn)
        for track, row in zip(matched, remote):
            if row.get('isAvailable') is False:
                accepted.discard(track['video_id'])
            elif track['video_id'] != row['videoId']:
                substitution = substitutions.substitute(track, row, s.candidates.by_video,
                                                       identity_id=s.identity, playlist=s.playlist)
                actual = substitution.accepted
                s.candidates.retain([actual])
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

    def resume(self, playing, clients, candidates_factory):
        state = self.persistence.load()
        if not state or state.get("stopped"):
            return None, False
        identity = next((key for key in clients if str(key) == str(state["identity"])), None)
        if (identity is None or not state.get("playlist")
                or state["playlist"] != playing.get("playlist_ytm_id")
                or not (state.get("tracks") or state.get("initial_tracks"))):
            return None, False
        candidates = candidates_factory(self.store, self.now_fn(), state.get("tuning"))
        return restore(state, clients[identity], identity, candidates), state.get("fingerprint") != candidates.fingerprint

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
            self.playlists.publish(s.local_id, s.title, s.tracks, seeds, self.now_fn())
            s.accept_tracks(seeds, self.now_fn())
        s.initial_tracks = []
        s.initial_handles = {}
        s.preparation_deadline = 0
        s.preparation_snapshot = None
        if not s.stopped_by_user:
            s.move(Phase.AWAITING_PLAYBACK)
        s.launch_deadline = time.monotonic() + 15
        s.error = None

    def adopt(self, playing, clients, candidates_factory):
        """Recognize an older already playing session without creating or navigating."""
        playlist = playing.get("playlist_ytm_id")
        known = json.loads(self.store.settings.get_setting("radio_session_playlist_ids") or "[]")
        if not playlist or playlist not in known:
            return None
        local = next((p for p in self.store.playlists.get_playlists() if p.ytm_playlist_id == playlist), None)
        if local is None or local.identity_id not in clients:
            return None
        candidates = candidates_factory(self.store, self.now_fn())
        candidates.retain([{**t, "key": t["identity_key"]}
                           for t in self.store.charts.playlist_tracks_detail(local.id)])
        ids = [t["videoId"] for t in self.playlists.read(clients[local.identity_id], playlist)]
        if not ids or any(video not in candidates.by_video for video in ids):
            return None
        s = Session(clients[local.identity_id], candidates, local.identity_id, local.title)
        s.playlist, s.local_id, s.tracks = playlist, local.id, [candidates.by_video[video] for video in ids]
        s.selected = list(s.tracks)
        s.position = next((i for i, t in enumerate(s.tracks) if t["video_id"] == playing.get("video_id")), -1)
        s.furthest = s.position
        s.move(Phase.AWAITING_PLAYBACK)
        s.playback_confirmed, s.started_at = True, self.now_fn()
        s.settle()
        s.directions = RadioDirections(candidates, s.tracks[0])
        return s
