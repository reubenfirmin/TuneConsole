"""Versioned radio checkpoints. Restoring never performs a remote read or write."""
import json
import threading
import time

from yt_playlist.core import radio_checkpoint

from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.web.radio_edits import AppendOperation, PlaylistOperation
from yt_playlist.web.radio_playlist_io import PendingWrite, PlaylistEdit
from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail
from yt_playlist.web.radio_state import Phase, Session

SESSION_KEY = "radio_current_session"
PREPARATION_TIMEOUT = 60


def migrate(state: dict) -> dict:
    """v1 used overlapping flags; v2 stores one explicit lifecycle phase."""
    state = radio_checkpoint.normalize(state)
    state["phase"] = Phase(state["phase"])
    return state


def operation_values(operation: PlaylistOperation | None) -> dict:
    """Keep the v2 wire format while restoring it into one legal operation in memory."""
    append = operation if isinstance(operation, AppendOperation) else None
    tail = operation if isinstance(operation, RadioPlaylistTail) else None
    return {
        'pending': append.track if append else None,
        'pending_sent': bool(append and append.write),
        'pending_result': append.write.receipt if append and append.write else None,
        'pending_started_at': append.write.started_at if append and append.write else None,
        'replacement': {'allowed': [list(ids) for ids in sorted(tail.allowed)],
            'confirmed': list(tail.confirmed),
            'waiting': list(tail.waiting) if tail.waiting is not None else None,
            'waiting_at': tail.write.started_at if tail.write else None,
            'receipt': tail.receipt, 'adding': tail.adding, 'proposed': tail.proposed,
            'play_prefix': list(tail.play_prefix) if tail.play_prefix is not None else None,
            'slot': tail.slot, 'choice_id': tail.choice_id, 'excluded': tail.excluded} if tail else None,
    }


def encode(s: Session) -> str:
    directions = s.directions
    state = {"version": 2, "phase": s.phase.value, "playlist": s.playlist, "local_id": s.local_id,
        "identity": s.identity, "title": s.title, "episode_id": s.episode_id,
        "started_at": s.started_at, "tracks": s.tracks, "position": s.position, "furthest": s.furthest,
        "fingerprint": s.candidates.fingerprint, "heard": s.heard, "transition": s.transition,
        **operation_values(s.edits.active), "initial_tracks": s.initial_tracks,
        "initial_handles": s.initial_handles, "launch_id": s.launch_id,
        "tuning": s.candidates.tuning, "recent_states": s.edits.recent_states,
        'comparison': getattr(s.candidates, 'comparison', None),
        'candidate_context': {'choice_context': getattr(s.candidates, 'choice_context', {}),
            'now_posterior': getattr(s.candidates, 'now_posterior', {}),
            'now_sampled_at': getattr(s.candidates, 'now_sampled_at', 0),
            'now_gain': getattr(s.candidates, 'now_gain', 0.),
            'encoding_model': getattr(s.candidates, 'encoding_model', None)},
        "requested_direction": ({k: v for k, v in s.requested_direction.items() if k != 'play'}
                                if s.requested_direction else None),
        "choices": s.choices, "rejected": sorted(s.rejected),
        "stopped": s.stopped_by_user, "halted": s.halted, "error": s.error,
        "directions": {"active": directions.active, "slots": directions.slots,
            "revision": directions.revision, "menu_id": directions.menu_id,
            "feedback": directions.feedback, "decay": directions.decay} if directions else None}
    return json.dumps(state, sort_keys=True)


def restore(state: dict, client, identity, candidates) -> Session:
    state = migrate(state)
    s = Session(client, candidates, identity, state["title"], phase=state["phase"])
    for key in ("playlist", "local_id", "episode_id", "started_at", "tracks", "position",
                "heard", "transition", "choices"):
        setattr(s, key, state[key])
    if state.get('pending'):
        append = AppendOperation(state['pending'])
        if state.get('pending_sent'):
            after = tuple(t['video_id'] for t in s.tracks) + (append.track['video_id'],)
            append.write = PendingWrite(PlaylistEdit('add', after, track=append.track),
                                        state.get('pending_started_at'), state.get('pending_result'))
        s.edits.begin(append)
    s.selected = list(s.tracks) + ([s.pending] if s.pending else [])
    s.furthest = state.get("furthest", s.position)
    s.initial_tracks = state.get("initial_tracks", [])
    s.initial_handles = state.get("initial_handles", {})
    s.launch_id = state.get("launch_id")
    s.edits.recent_states = state.get("recent_states", [])
    s.edits.requested = state.get("requested_direction")
    s.rejected = set(state.get("rejected", []))
    s.error = state.get("error")
    if s.halted and not s.error:
        s.error = "The playlist was changed elsewhere. Radio stopped to preserve those changes."
    if s.initial_tracks:
        s.preparation_deadline = time.monotonic() + PREPARATION_TIMEOUT
    s.playback_confirmed = True
    # The last heard entry is the last observed song, even when a queued
    # replacement moved its position. Rejoining that same song is not a new
    # listen; a genuinely different observation still records a transition.
    s.observed_video = s.heard[-1]['video_id'] if s.heard else None
    s.directions = RadioDirections(candidates, (s.tracks or s.initial_tracks)[0])
    saved_directions = state.get("directions")
    if saved_directions:
        for key in ("active", "slots", "revision", "menu_id", "feedback"):
            setattr(s.directions, key, saved_directions[key])
        s.directions.decay = {int(k): v for k, v in saved_directions.get("decay", {}).items()}
        s.directions.transition = s.transition
    metadata = list(s.tracks) + s.heard + s.initial_tracks + ([s.pending] if s.pending else [])
    metadata += [t for branch in s.directions.slots if branch for t in branch["tracks"]]
    candidates.retain(metadata)
    pending = state.get("replacement")
    if pending:
        tail = RadioPlaylistTail(s.tracks, pending["proposed"], pending["slot"],
                                 choice_id=pending.get("choice_id"), excluded=pending.get('excluded', ()),
                                 play_prefix=pending.get('play_prefix'))
        s.edits.begin(tail)
        tail.allowed = {tuple(ids) for ids in pending["allowed"]}
        tail.confirmed = tuple(pending.get("confirmed", [t["video_id"] for t in s.tracks]))
        if pending.get('waiting') is not None:
            after = tuple(pending['waiting'])
            kind = 'add' if pending.get('adding') else 'remove' if len(after) < len(tail.confirmed) else 'move'
            tail.write = PendingWrite(PlaylistEdit(kind, after, track=pending.get('adding')),
                                      pending.get('waiting_at'), pending.get('receipt'))
        candidates.retain(pending["proposed"])
    s.directions.blocked.update(s.rejected)
    if s.stopped_by_user:
        s.stop.set()
    return s


class RadioPersistence:
    def __init__(self, checkpoints, *, paused=lambda: False, snapshot=encode):
        self.checkpoints = checkpoints
        self.paused = paused
        self.snapshot = snapshot
        self.transient = None
        self.last_saved = None
        self.lock = threading.RLock()
        state, self.revision = checkpoints.load()
        self.legacy_checkpoint = state if self.revision is None else None

    def load(self) -> dict | None:
        if self.transient is not None:
            return migrate(json.loads(self.transient))
        state, self.revision = self.checkpoints.load()
        self.legacy_checkpoint = state if self.revision is None else None
        return migrate(state) if state else None

    def view(self, reader) -> dict | None:
        """Project a committed page snapshot without changing our write token."""
        if self.transient is not None:
            return migrate(json.loads(self.transient))
        state, _ = reader.radio_checkpoints.load()
        return migrate(state) if state else None

    def save(self, session: Session):
        with self.lock:
            # Capture after acquiring save/journal ownership, so a waiting save
            # cannot publish a snapshot older than an intervening Stop or edit.
            # The snapshot callback releases its live-state lock before I/O.
            encoded = self.snapshot(session)
            if encoded is not None:
                self._write(encoded)

    def record_edit(self, episode_id: str, operation: PlaylistOperation):
        """Journal only this episode's edit, retaining a concurrent Stop or newer choice."""
        with self.lock:
            state = self.load()
            if not state or state.get('episode_id') != episode_id:
                raise ValueError('Radio playlist operation belongs to a superseded session')
            state.update(operation_values(operation))
            self._write(json.dumps(state, sort_keys=True))

    def stop_launch(self, launch_id: str):
        """Cancel the matching saved launch without requiring live playback."""
        with self.lock:
            state = self.load()
            if not state or state.get('launch_id') != launch_id or state.get('stopped'):
                return
            # Retain queue contents and any uncertain operation journal. The
            # ordinary compare-and-save token fences a concurrent replacement.
            state.update(phase=Phase.STOPPED, stopped=True, halted=False)
            self._write(json.dumps(state, sort_keys=True))

    def _write(self, encoded):
        # Keep the live transaction journal in RAM during repair. A session
        # created without a local playlist remains transient for its lifetime.
        state = json.loads(encoded)
        if self.paused() or ('local_id' in state and state['local_id'] is None):
            self.transient = encoded
            return
        if encoded != self.last_saved or self.transient is not None:
            self.revision = self.checkpoints.save(encoded, expected=self.revision,
                                                 predecessor=self.legacy_checkpoint)
            self.legacy_checkpoint = state if self.revision is None else None
        if encoded != self.last_saved:
            self.last_saved = encoded
        self.transient = None

