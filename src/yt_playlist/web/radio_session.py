"""Saved-playlist radio, observed through the extension's native playback state.

Start with three songs in a new YouTube playlist; then keep one song ahead of
the currently playing song. Ask Music's native refresh handler to fetch the saved
playlist, without supplying playback items or reloading the tab.
"""
import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlencode
from uuid import uuid4

from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for
from yt_playlist.core.bridge import BridgeError
from yt_playlist.library import executor
from yt_playlist.library.playlist_metadata import content_hash
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.listen_derive import classify_exit
from yt_playlist.rec import measurement
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec import radio_tuning
from yt_playlist.web.radio_playlist_tail import LOOKAHEAD, PlaylistChanged, PlaylistPending, RadioPlaylistTail

logger = logging.getLogger(__name__)
INITIAL_TRACKS = 3
PREPARATION_TIMEOUT = 60


@dataclass
class Session:
    client: object
    candidates: RadioCandidates
    identity: object
    title: str
    stop: threading.Event = field(default_factory=threading.Event)
    selected: list = field(default_factory=list)
    playlist: str | None = None
    local_id: int | None = None
    tracks: list = field(default_factory=list)
    position: int = -1
    furthest: int = -1
    pending: dict | None = None
    pending_sent: bool = False
    initial_tracks: list = field(default_factory=list)
    recent_states: list = field(default_factory=list)
    error: str | None = None
    exhausted: bool = False
    starting: bool = True
    browser: dict | None = None
    browser_seen_at: float | None = None
    refresh_needed: bool = False
    directions: RadioDirections | None = None
    replacement: RadioPlaylistTail | None = None
    requested_direction: dict | None = None
    episode_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = 0
    transition: int = 0
    observed_video: str | None = None
    starts: dict = field(default_factory=dict)
    feedback_seen: set = field(default_factory=set)
    seen_menus: set = field(default_factory=set)
    choices: dict = field(default_factory=dict)
    mutation_windows: list = field(default_factory=list)
    heard: list = field(default_factory=list)
    rejected: set = field(default_factory=set)
    stopped_by_user: bool = False
    halted: bool = False
    monitoring: bool = False
    feedback_dirty: bool = False
    observer_error: str | None = None
    observer_report: tuple | None = None
    launch_deadline: float = 0
    preparation_deadline: float = 0
    playback_confirmed: bool = False
    readback_conflict: tuple | None = None
    readback_conflict_at: float = 0
    enrichment_requested: set = field(default_factory=set)
    metadata_checked_at: float = 0
    metadata_revision: int = 0
    metadata_rejected: set = field(default_factory=set)


class PlaylistRadio:
    def __init__(self, ctx):
        self.ctx = ctx
        self.work = activity_for(ctx)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.session = None
        self.offer = None
        self.offer_id = None
        self.saved_state = None
        self.preparing = False
        self.checked_playlist = None

    def _playing(self):
        """The extension's fresh playback presence is authoritative, not saved state."""
        bridge = self.ctx.bridge
        playing = bridge.now_playing or {}
        seen = getattr(bridge, "now_playing_seen_at", None)
        if (not bridge.connected or seen is None or time.monotonic() - seen > 8
                or playing.get("paused") or not playing.get("video_id")):
            return {}
        return playing

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

    @staticmethod
    def _selected_slot(s):
        if s.requested_direction:
            return s.requested_direction["slot"]
        if s.replacement:
            return s.replacement.slot
        return s.directions.active if s.directions else None

    def prepare(self):
        """Warm recommendations and reattach off the page-rendering path."""
        with self.lock:
            playing = self._playing().get("playlist_ytm_id")
            settled = (self.session and (self.session.monitoring or self.session.stopped_by_user
                        or not self._is_current(self.session))) or (
                        self.session is None and playing == self.checked_playlist)
            policy_due = bool(self.session and not self.session.stopped_by_user
                and self.ctx.now_fn() - self.session.candidates.now_sampled_at >= radio_tuning.NOW_REFRESH_SECONDS)
            if self.preparing or not self.work.accepting or (self.offer is not None and settled and not policy_due):
                return
            self.preparing = True
        def warm():
            try:
                self.join()
                if policy_due and self.session:
                    self.session.candidates.refresh_now(self.ctx.now_fn())
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
            raw = self.ctx.store.settings.get_setting("radio_current_session")
            try:
                saved = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                saved = {}
            plan = saved.get("directions") or {}
            tracks = saved.get("tracks", [])
            scheduled = {t["video_id"] for t in tracks[max(0, saved.get("furthest", -1)) + 1:]}
            replacement = saved.get("requested_direction") or saved.get("replacement")
            active = bool(saved.get("playlist") and self._playing().get("playlist_ytm_id") == saved["playlist"]
                          and not saved.get("stopped"))
            selected = replacement["slot"] if replacement else plan.get("active", 0)
            state = {"active": active,
                "starting": bool(active and saved.get("initial_tracks")), "local_id": saved.get("local_id"),
                "playlist_id": saved.get("playlist"), "title": saved.get("title"), "tracks": tracks,
                "position": saved.get("position", -1), "menu_id": plan.get("menu_id"),
                "tuning": saved.get("tuning", radio_tuning.DEFAULTS), "metadata_revision": 0,
                "history_count": min(len(tracks), max(1, saved.get("furthest", -1) + 1,
                                                     saved.get("position", -1) + 1)),
                "updating": bool(replacement), "pending_slot": replacement["slot"] if replacement else None,
                "directions": [{**branch, "slot": slot, "active": slot == selected,
                    "decay_total": 3, "decay_remaining": (3 if slot == plan.get("active", 0) else
                        max(0, plan.get("decay", {}).get(str(slot), saved.get("transition", 0) + 3)
                            - saved.get("transition", 0))),
                    "tracks": [{**t, "saved": t["video_id"] in scheduled} for t in branch["tracks"]]}
                    for slot, branch in enumerate(plan.get("slots", [])) if branch]}
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
            directions, replacement = s.directions, s.replacement
            state = {"version": 1, "playlist": s.playlist, "local_id": s.local_id,
                "identity": s.identity, "title": s.title, "episode_id": s.episode_id,
                "started_at": s.started_at, "tracks": s.tracks, "position": s.position,
                "furthest": s.furthest,
                "fingerprint": s.candidates.fingerprint,
                "heard": s.heard, "transition": s.transition, "pending": s.pending,
                "pending_sent": s.pending_sent, "initial_tracks": s.initial_tracks,
                "tuning": s.candidates.tuning,
                "recent_states": s.recent_states,
                "requested_direction": s.requested_direction,
                "choices": s.choices, "rejected": sorted(s.rejected),
                "stopped": s.stopped_by_user, "halted": s.halted,
                "directions": {"active": directions.active, "slots": directions.slots,
                    "revision": directions.revision, "menu_id": directions.menu_id,
                    "feedback": directions.feedback, "decay": directions.decay} if directions else None,
                "replacement": {"allowed": [list(ids) for ids in replacement.allowed],
                    "confirmed": list(replacement.confirmed),
                    "waiting": list(replacement.waiting) if replacement.waiting is not None else None,
                    "proposed": replacement.proposed, "slot": replacement.slot,
                    "choice_id": replacement.choice_id} if replacement else None}
            encoded = json.dumps(state, sort_keys=True)
            if encoded != self.saved_state:
                self.ctx.store.settings.set_setting("radio_current_session", encoded)
                self.saved_state = encoded

    def join(self):
        """Attach the page to the saved session without navigation or playlist creation."""
        with self.lock:
            s = self.session
            if s and (s.monitoring or s.stopped_by_user or not self.ctx.bridge.connected):
                return self.status()
        with self.operation:
            if self.session is None:
                raw = self.ctx.store.settings.get_setting("radio_current_session")
                if raw:
                    try:
                        state = json.loads(raw)
                        clients = self.ctx.client_provider() or {}
                        # JSON can round-trip a numeric identity as a string in some
                        # older settings; match the provider's actual identity key.
                        identity = next((key for key in clients if str(key) == str(state["identity"])), None)
                        if (state.get("version") == 1 and identity is not None
                                and not state.get("stopped")
                                and state.get("playlist") and state["playlist"] == self._playing().get("playlist_ytm_id")
                                and (state.get("tracks") or state.get("initial_tracks"))):
                            candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn(), state.get("tuning"))
                            s = Session(clients[identity], candidates, identity, state["title"])
                            for key in ("playlist", "local_id", "episode_id", "started_at", "tracks",
                                        "position", "heard", "transition", "pending", "choices"):
                                setattr(s, key, state[key])
                            s.selected = list(s.tracks) + ([s.pending] if s.pending else [])
                            s.furthest = state.get("furthest", s.position)
                            s.pending_sent = state.get("pending_sent", False)
                            s.initial_tracks = state.get("initial_tracks", [])
                            s.recent_states = state.get("recent_states", [])
                            s.requested_direction = state.get("requested_direction")
                            s.rejected = set(state.get("rejected", []))
                            s.stopped_by_user = state.get("stopped", False)
                            s.halted = state.get("halted", False)
                            s.starting = bool(s.initial_tracks)
                            if s.initial_tracks:
                                s.preparation_deadline = time.monotonic() + PREPARATION_TIMEOUT
                            s.playback_confirmed = True
                            s.directions = RadioDirections(candidates, (s.tracks or s.initial_tracks)[0])
                            saved_directions = state.get("directions")
                            if saved_directions:
                                for key in ("active", "slots", "revision", "menu_id", "feedback"):
                                    setattr(s.directions, key, saved_directions[key])
                                s.directions.decay = {int(k): v for k, v in saved_directions.get("decay", {}).items()}
                                s.directions.transition = s.transition
                            # Historical songs may now be disliked or outside the
                            # candidate pool. Retain their metadata for readback.
                            metadata = list(s.tracks) + s.initial_tracks + ([s.pending] if s.pending else [])
                            metadata += [t for branch in s.directions.slots if branch for t in branch["tracks"]]
                            candidates.retain(metadata)
                            pending = state.get("replacement")
                            if pending:
                                s.replacement = RadioPlaylistTail(s.tracks, pending["proposed"], pending["slot"],
                                                                  choice_id=pending.get("choice_id"))
                                s.replacement.allowed = {tuple(ids) for ids in pending["allowed"]}
                                s.replacement.confirmed = tuple(pending.get("confirmed", [t["video_id"] for t in s.tracks]))
                                waiting = pending.get("waiting")
                                s.replacement.waiting = tuple(waiting) if waiting is not None else None
                                candidates.retain(pending["proposed"])
                            if s.stopped_by_user:
                                s.stop.set()
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
        s.starting = False
        s.playback_confirmed = True
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
            directions = s.directions.public(s.tracks[max(0, s.furthest) + 1:]) if s and s.directions else []
            selected = self._selected_slot(s) if s else None
            for branch in directions:
                branch["active"] = branch["slot"] == selected
            return {
                "active": bool(self._is_current(s)),
                "starting": bool(s and (s.starting or time.monotonic() < s.launch_deadline)),
                "playlist_id": s.playlist if s else None,
                "local_id": s.local_id if s else None,
                "title": s.title if s else None,
                "playlist_url": (f"https://music.youtube.com/playlist?list={s.playlist}"
                                 if s and s.playlist else None),
                "position": s.position if s else -1,
                "ahead": max(0, len(s.tracks) - s.position - 1) if s and s.position >= 0 else None,
                "tracks": list(s.tracks) if s else [],
                "history_count": min(len(s.tracks), max(1, s.furthest + 1, s.position + 1)) if s else 0,
                "error": s.error if s else None,
                "observer_error": s.observer_error if s else None,
                "exhausted": bool(s and s.exhausted),
                "connected": self.ctx.bridge.connected,
                "browser": s.browser if s else None,
                "browser_age": (time.monotonic() - s.browser_seen_at
                                if s and s.browser_seen_at is not None else None),
                "directions": directions,
                "menu_id": s.directions.menu_id if s and s.directions else None,
                "revision": s.directions.revision if s and s.directions else 0,
                "tuning": dict(s.candidates.tuning) if s else dict(radio_tuning.DEFAULTS),
                "metadata_revision": s.metadata_revision if s else 0,
                "updating": bool(s and (s.replacement or s.requested_direction)),
                "pending_slot": (s.requested_direction["slot"] if s and s.requested_direction else
                                 s.replacement.slot if s and s.replacement else None),
            }

    def stop(self):
        with self.lock:
            if self.session:
                self.session.stopped_by_user = True
                self.session.stop.set()
                self._persist(self.session)
        return {"ok": True, **self.status()}

    def suspend(self):
        """A disconnected extension can reattach; it is not an explicit Stop."""
        with self.lock:
            if self.session:
                self.session.stop.set()

    def _pick(self, s):
        if s.directions:
            used = {t["video_id"] for t in s.selected} | s.directions.blocked
            active = s.directions.slots[s.directions.active]
            pick = next((t for t in active["tracks"] if t["video_id"] not in used), None)
        else:
            pick = s.candidates.next_track(s.selected)
        if pick is not None:
            s.selected.append(pick)
        return pick

    def _mirror(self, s, tracks):
        """Publish only confirmed saved-playlist contents through existing repo APIs."""
        store, now = self.ctx.store, self.ctx.now_fn()
        track_ids = [store.tracks.upsert_track(
            t["video_id"], t.get("title") or "", t.get("artist") or "",
            t.get("album") or "", t.get("duration"), thumbnail=t.get("thumbnail"))
            for t in tracks]
        keys = [store.tracks.catalog_subject(tid)["identity_key"] for tid in track_ids]
        store.playlists.upsert_playlist(s.identity, s.playlist, s.title, len(track_ids),
                                       content_hash(list(dict.fromkeys(keys))), now)
        store.playlists.set_playlist_tracks(s.local_id, track_ids)

    def _remote_tracks(self, s, *, expected=None):
        detail = s.client.get_playlist(s.playlist, limit=None)
        # A newly created playlist's header count can lag behind its contents.
        # An exact readback of every accepted upload, in order, proves readiness
        # without relying on that header. Keep normal mutation reads strict.
        if (expected is not None and isinstance(detail, dict) and isinstance(detail.get("tracks"), list)
                and all(isinstance(t, dict) for t in detail["tracks"])
                and [t.get("videoId") for t in detail["tracks"]] == expected):
            detail = {**detail, "trackCount": len(expected)}
        try:
            return validate_playlist(detail)["tracks"]
        except ValueError as exc:
            # Counts and rows can disagree immediately after a write. Retain the
            # last complete snapshot and let the observer read again quietly.
            if s.initial_tracks:
                report = ("preparing", str(exc), detail.get("trackCount") if isinstance(detail, dict) else None)
                if report != s.observer_report:
                    logger.info("Radio playlist preparation pending: playlist=%s reason=%s count=%s",
                                s.playlist, exc, report[2])
                    s.observer_report = report
            else:
                logger.debug("Radio playlist readback pending: %s", exc)
            raise PlaylistPending() from exc

    def _remote_ids(self, s):
        return [t["videoId"] for t in self._remote_tracks(s)]

    def _stale_read(self, s, ids):
        now = self.ctx.now_fn()
        s.recent_states = [state for state in s.recent_states if now - state["at"] < 60]
        return any(state["ids"] == ids for state in s.recent_states)

    def _publish(self, s, tracks):
        """Only complete, confirmed remote reads can become the local playlist UI."""
        if [t["video_id"] for t in tracks] != [t["video_id"] for t in s.tracks]:
            self._mirror(s, tracks)
            with self.lock:
                now = self.ctx.now_fn()
                s.recent_states = [state for state in s.recent_states if now - state["at"] < 60]
                s.recent_states.append({"ids": [t["video_id"] for t in s.tracks], "at": now})
                s.tracks = list(tracks)
                s.selected = list(tracks)

    def _browser(self, s):
        # A replacement can involve several saved-playlist edits. Only ask the
        # native player to fetch the playlist after the final state is saved.
        refresh = s.refresh_needed and not s.requested_direction and (not s.replacement or s.replacement.saved)
        refresh_at = self.ctx.now_fn() if refresh else None
        payload = {"playlist": s.playlist, "videoIds": [t["video_id"] for t in s.tracks],
                   "refresh": refresh}
        try:
            status, body = asyncio.run(self.ctx.bridge.request_control({"type": "radio-session", **payload}))
            result = json.loads(body)
            # An already running service worker can still use the previous
            # command name. Negotiate it without asking the user to reload.
            if status == 0 and result.get("error") == "blocked by allowlist":
                status, body = asyncio.run(self.ctx.bridge.request_control({"type": "playlist-session-poc", **payload}))
                result = json.loads(body)
            if status != 200 or not result.get("ok"):
                reason = result.get("error") or f"observer response {status}"
                if reason != s.observer_error:
                    logger.warning("Radio observer pending: %s", reason)
                s.observer_error = reason
                raise PlaylistPending()
        except (BridgeError, ValueError) as exc:
            s.observer_error = str(exc)
            logger.debug("Radio observer reconnecting: %s", exc)
            raise PlaylistPending() from exc
        with self.lock:
            s.observer_error = None
            s.error = None
            s.browser = result
            s.browser_seen_at = time.monotonic()
            player = result.get("player") or {}
            if (player.get("playlist_ytm_id") == s.playlist and player.get("video_id")
                    and not player.get("paused")):
                s.playback_confirmed = True
                s.launch_deadline = 0
            # A refresh request alone is not acknowledgement. Keep checking the
            # actual future, including if a late response restores an older one.
            s.refresh_needed = not bool(result.get("playback_synced"))
            if refresh_at is not None and result.get("refresh") in {"clicked", "requested"}:
                s.mutation_windows.append([refresh_at, self.ctx.now_fn() + 3])
            self._report_playback(s, result)

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
        player = s.browser.get("player", {}) if s.browser else {}
        if (player.get("paused") or player.get("playlist_ytm_id") != s.playlist
                or s.browser_seen_at is None or time.monotonic() - s.browser_seen_at > 8):
            return None
        return player

    def _observe(self, s, position):
        video = s.tracks[position]["video_id"]
        now = self.ctx.now_fn()
        with self.lock:
            s.position = position
            s.furthest = max(s.furthest, position)
            if s.directions:
                s.directions.retired = {t["video_id"] for t in s.tracks[:s.furthest + 1]}
            if s.observed_video == video:
                return
            if s.observed_video:
                s.transition += 1
            s.observed_video = video
            s.starts[video] = now
            s.heard.append(s.tracks[position])
            choice = s.choices.pop(video, None)
        # Count an explicit seed/branch choice once when its first song starts.
        # Passive continuation tracks remain generated listening in the live-play pipeline.
        if choice:
            measurement.record_action(self.ctx.store, "start", now,
                keys=[s.tracks[position]["key"]],
                event_id=f"radio:{s.episode_id}:{choice['menu_id']}:{choice['id']}")

    def _update_directions(self, s, *, force=False):
        with self.lock:
            position = max(0, s.furthest)
            s.directions.retired = {t["video_id"] for t in s.tracks[:position + 1]}
            s.directions.update(s.heard or s.tracks[:1], s.tracks[position + 1:],
                                s.transition, force=force or s.feedback_dirty)
            s.feedback_dirty = False

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
            keys = {t["key"] for branch in branches for t in branch["tracks"]
                    if not t.get("genre") or not t.get("year")}
            missing = keys - s.enrichment_requested
            if worker and missing:
                worker.prioritize(missing)
                s.enrichment_requested.update(missing)
            return {"ok": True}

    def _refresh_metadata(self, s):
        if not s.seen_menus or time.monotonic() - s.metadata_checked_at < 6:
            return
        s.metadata_checked_at = time.monotonic()
        tracks = [t for branch in s.directions.slots if branch for t in branch["tracks"]]
        tracks += s.tracks[max(0, s.furthest) + 1:]
        if not s.candidates.refresh_metadata(tracks):
            return
        s.metadata_revision += 1
        for branch in s.directions.slots:
            if branch:
                s.metadata_rejected.update(t["video_id"] for t in branch["tracks"]
                    if not s.candidates.fits(t, branch["anchor"]))
        if s.metadata_rejected - s.directions.blocked:
            s.directions.blocked.update(s.metadata_rejected)
            s.feedback_dirty = True

    def _feedback(self, s):
        """Read existing feedback/events; no new schema or inferred pause/disconnect skips."""
        store, now = self.ctx.store, self.ctx.now_fn()
        directions = s.directions
        changed = False
        disliked = store.rec_model.disliked_identity_keys()
        muted = {a.casefold() for a in store.rec_model.muted_artists()}
        suppressed = store.rec_model.suppressed_keys("for_you", now)
        blocked = s.rejected | s.metadata_rejected | {t["video_id"] for t in s.candidates.by_video.values()
            if t["key"] in disliked | suppressed
            or (t.get("artist") or "").casefold() in muted}
        if directions.blocked != blocked:
            directions.blocked = blocked
            changed = True
        ratings = [(key, at, .45, "like") for key, at in store.rec_model.recent_liked_with_ts(limit=200)]
        ratings += [(key, at, -.7, "dislike") for key, at in store.rec_model.disliked_with_ts()]
        for key, at, strength, kind in ratings:
            token = (kind, key, at)
            if at >= s.started_at and key in s.candidates.by_key and token not in s.feedback_seen:
                s.feedback_seen.add(token)
                directions.feedback[key] = strength
                changed = True
        for event in store.player_events.player_events_since(max(s.started_at, now - 45), kind="track_exit"):
            video, at = event["video_id"], event["at"]
            token = ("exit", video, at, event["position"], event["duration"])
            if (event["playlist_ytm_id"] != s.playlist or event["identity_id"] != s.identity
                    or video not in s.starts or at < s.starts[video] or video == s.observed_video
                    or token in s.feedback_seen):
                continue
            s.feedback_seen.add(token)
            if any(start <= at <= end for start, end in s.mutation_windows):
                continue
            if classify_exit(event["position"], event["duration"]) != "skip":
                continue
            key = s.candidates.by_video[video]["key"]
            directions.feedback[key] = max(-.6, directions.feedback.get(key, 0) - .18)
            changed = True
        s.feedback_dirty = s.feedback_dirty or changed
        return changed

    def _invalid(self, s, track):
        if track["video_id"] in s.directions.blocked:
            return True
        if track["key"] not in s.candidates.by_key:
            return True
        # One observed skip is a soft preference. Several nearby skips can
        # invalidate a scheduled continuation without banning a whole genre.
        negative = sum(-strength * max(0, (s.candidates.similarity(
            track, s.candidates.by_key[key]) - .55) / .45)
            for key, strength in s.directions.feedback.items()
            if strength < 0 and key in s.candidates.by_key)
        return negative >= .32

    def _finish_replacement(self, s):
        replacement = s.replacement
        if not replacement.apply(self, s):
            return False
        with self.lock:
            # Finish an in-flight saved edit before applying the latest choice,
            # without waiting for playback to adopt a superseded direction.
            if s.requested_direction:
                s.replacement = None
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
            s.replacement = None
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
            if slot == self._selected_slot(s):
                return {"ok": True, **self.status()}
            if not branch["tracks"]:
                return {"ok": False, "reason": "No more library songs are available in that direction."}
            s.requested_direction = {"slot": slot, "tracks": list(branch["tracks"]),
                                     "choice": {"id": branch["id"], "menu_id": menu_id}}
            s.error = None
            self._persist(s)
            return {"ok": True, **self.status()}

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
            candidates = RadioCandidates(self.ctx.store, self.ctx.now_fn(), values)
            metadata = list(s.tracks) + [t for b in s.directions.slots if b for t in b["tracks"]]
            if s.replacement:
                metadata += s.replacement.proposed
            candidates.retain(metadata)
            with self.lock:
                selected = self._selected_slot(s)
                choice = (s.requested_direction["choice"] if s.requested_direction else
                          s.replacement.choice_id if s.replacement else None)
                s.candidates = s.directions.candidates = candidates
                s.directions.blocked.difference_update(s.metadata_rejected - s.rejected)
                s.metadata_rejected.clear()
                s.directions.update(s.heard or s.tracks[:1], [], s.transition, force=True)
                branch = s.directions.slots[selected]
                if branch["tracks"]:
                    s.requested_direction = {"slot": selected, "tracks": list(branch["tracks"]),
                                             "choice": choice}
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
            with self.lock:
                if self.session:
                    self.session.stop.set()
                s = self.session = Session(client, candidates, identity, f"Radio Session · {day.isoformat()}")
                s.started_at = self.ctx.now_fn()
                s.selected = list(seeds or [seed])
                s.choices[seed["video_id"]] = {"id": uuid4().hex,
                    "menu_id": f"playlist:{playlist_id}" if playlist_id is not None else menu_id or self.offer_id or "direct"}
            try:
                seeds = list(s.selected)
                for _ in range(max(0, INITIAL_TRACKS - len(seeds))):
                    if s.stop.is_set():
                        return {"ok": False, "reason": "Stopped during start."}
                    pick = self._pick(s)
                    if pick is None and playlist_id is not None:
                        break
                    if pick is None:
                        raise ValueError("Fewer than three playable songs are available. Sync your YouTube Music library first.")
                    seeds.append(pick)
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
                    s.halted = True
                    s.stop.set()
                    self._persist(s)
                return {"ok": False, "reason": str(exc), **self.status()}
            finally:
                with self.lock:
                    s.starting = bool(s.initial_tracks) and not s.halted
            return {"ok": True, **self.status()}

    def _complete_start(self, s):
        """Wait for the existing playlist to settle, without recreating it."""
        seeds = s.initial_tracks
        if s.preparation_deadline and time.monotonic() >= s.preparation_deadline:
            raise PlaylistChanged("Radio playlist preparation timed out.")
        expected = [t["video_id"] for t in seeds]
        received = [t["videoId"] for t in self._remote_tracks(s, expected=expected)]
        if received != expected:
            report = ("preparing", tuple(received))
            if report != s.observer_report:
                logger.info("Radio playlist preparation pending: playlist=%s expected=%s received=%s",
                            s.playlist, expected, received)
                s.observer_report = report
            raise PlaylistPending()
        self._publish(s, seeds)
        with self.lock:
            s.initial_tracks = []
            s.preparation_deadline = 0
            s.starting = False
            s.launch_deadline = time.monotonic() + 15
            s.error = None
            self._update_directions(s)
            self._persist(s)
            if not s.stop.is_set():
                self.ctx.bridge.send_control({"type": "navigate", "url":
                    "https://music.youtube.com/watch?" + urlencode({"v": seeds[0]["video_id"], "list": s.playlist})})

    def _monitor(self, s):
        try:
            while not s.stop.wait(2):
                if not self.work.accepting or not self.ctx.bridge.connected:
                    break
                try:
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
                                actual = tuple(self._remote_ids(s))
                                allowed = {tuple(t["video_id"] for t in s.tracks)}
                                if s.pending:
                                    allowed.add((*next(iter(allowed)), s.pending["video_id"]))
                                if s.replacement:
                                    allowed.update(s.replacement.allowed)
                                if actual not in allowed:
                                    continue
                                s.halted = False
                                s.readback_conflict = None
                                logger.info("Radio saved-playlist readback recovered: playlist=%s", s.playlist)
                            self._refresh_metadata(s)
                            self._advance(s)
                        self._persist(s)
                except PlaylistPending:
                    with self.lock:
                        s.error = None
                        self._persist(s)
                except PlaylistChanged as exc:
                    logger.warning("Radio saved-playlist conflict: playlist=%s current=%s reason=%s",
                                   s.playlist, s.observed_video, exc)
                    with self.lock:
                        s.error = str(exc)
                        s.halted = True
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
                self._persist(s)

    def advance(self, s):
        """Observe actual playback; append only to this session's saved playlist.

        Read back every addition. Keep an uncertain append pending so the next
        observation checks whether it landed before considering another write.
        """
        with self.operation:
            self._advance(s)

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
            if changed:
                replacement = s.replacement
                anchor = s.directions.slots[replacement.slot]["anchor"]
                valid = [t for t in replacement.proposed[:LOOKAHEAD] if not self._invalid(s, t)]
                replacement.proposed = s.directions._extend(s.heard, valid, anchor, ())
            if not self._finish_replacement(s):
                return
        if s.requested_direction:
            with self.lock:
                request = s.requested_direction
                s.replacement = RadioPlaylistTail(s.tracks, request["tracks"], request["slot"],
                                                  choice_id=request["choice"])
                s.choices.clear()
                if request["choice"]:
                    s.choices[request["tracks"][0]["video_id"]] = request["choice"]
                s.requested_direction = None
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
        invalid = [t for t in scheduled if self._invalid(s, t)]
        if invalid:
            s.rejected.update(t["video_id"] for t in invalid)
            s.directions.blocked.update(s.rejected)
            active = s.directions.slots[s.directions.active]
            valid = [t for t in scheduled if t not in invalid]
            proposal = s.directions._extend(s.heard, valid, active["anchor"], ())
            s.replacement = RadioPlaylistTail(s.tracks, proposal, s.directions.active)
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
            s.pending = self._pick(s)
            if s.pending is None:
                s.exhausted = True
                return False
        expected = [t["video_id"] for t in s.tracks]
        desired = expected + [s.pending["video_id"]]
        actual = self._remote_ids(s)
        if actual == expected and not s.pending_sent:
            if s.stop.is_set():
                return False
            if s.directions and s.pending["video_id"] in s.directions.blocked:
                s.selected = list(s.tracks)
                s.pending = None
                return True
            # Read back even after a lost response; never retry a write blindly.
            s.pending_sent = True
            self._persist(s)
            try:
                result = s.client.add_playlist_items(s.playlist, [s.pending["video_id"]])
                status = result.get("status") if isinstance(result, dict) else result
                if status and status != "STATUS_SUCCEEDED":
                    s.pending_sent = False
            except Exception:
                logger.warning("Radio Session append response failed; checking playlist", exc_info=True)
            actual = self._remote_ids(s)
        if actual == desired:
            s.readback_conflict = None
            self._publish(s, [*s.tracks, s.pending])
            with self.lock:
                s.pending = None
                s.pending_sent = False
                s.exhausted = False
                s.error = None
                s.refresh_needed = True
            self._persist(s)
            self._browser(s)
            return True
        if actual == expected or self._stale_read(s, actual):
            s.readback_conflict = None
            return False
        # A single complete but older read after reconnect is not an external
        # edit. Confirm the same conflict over several observations before
        # suspending writes; keep the established session visible throughout.
        conflict = tuple(actual)
        if s.readback_conflict != conflict:
            s.readback_conflict = conflict
            s.readback_conflict_at = time.monotonic()
            logger.info("Radio readback settling: playlist=%s expected=%s received=%s",
                        s.playlist, desired, actual)
        if time.monotonic() - s.readback_conflict_at < 30:
            raise PlaylistPending()
        raise PlaylistChanged("The playlist was changed elsewhere. Radio stopped to preserve those changes.")
