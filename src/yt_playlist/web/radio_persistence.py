"""Versioned radio checkpoints. Restoring never performs a remote read or write."""
import json
import threading
import time

from yt_playlist.core import radio_checkpoint, radio_storage

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
        "initial_handles": s.initial_handles,
        "tuning": s.candidates.tuning, "recent_states": s.edits.recent_states,
        'comparison': getattr(s.candidates, 'comparison', None),
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
    s.edits.recent_states = state.get("recent_states", [])
    s.edits.requested = state.get("requested_direction")
    s.rejected = set(state.get("rejected", []))
    s.error = state.get("error")
    if s.halted and not s.error:
        s.error = "The playlist was changed elsewhere. Radio stopped to preserve those changes."
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
    def __init__(self, settings, *, paused=lambda: False):
        self.settings = settings
        self.paused = paused
        self.transient = None
        self.last_saved = None
        self.lock = threading.RLock()
        self.revision = None
        self.legacy_checkpoint = None
        store = getattr(settings,'_store',None)
        if store is not None:
            with store._lock:
                if radio_storage.available(store.conn):
                    _,self.revision = radio_storage.load(store.conn)
                    return
        raw = settings.get_setting(SESSION_KEY)
        self.legacy_checkpoint = json.loads(raw) if raw else None

    def load(self) -> dict | None:
        store = getattr(self.settings,'_store',None)
        if self.transient is None and store is not None:
            with store._lock:
                # The schema probe also uses the shared writer connection. It
                # must be serialized with seed preparation and snapshot copies.
                if radio_storage.available(store.conn):
                    state,self.revision = radio_storage.load(store.conn)
                    self.legacy_checkpoint = None
                    return migrate(state) if state else None
        raw = self.transient if self.transient is not None else self.settings.get_setting(SESSION_KEY)
        if self.transient is None:
            self.legacy_checkpoint = json.loads(raw) if raw else None
        return migrate(json.loads(raw)) if raw else None

    def view(self, reader) -> dict | None:
        """Project a committed page snapshot without changing our write token."""
        if self.transient is not None:
            return migrate(json.loads(self.transient))
        with reader._lock:
            if radio_storage.available(reader.conn):
                state, _ = radio_storage.load(reader.conn)
                return migrate(state) if state else None
            raw = reader.settings.get_setting(SESSION_KEY)
        return migrate(json.loads(raw)) if raw else None

    def save(self, session: Session):
        with self.lock:
            self._write(encode(session))

    def record_edit(self, episode_id: str, operation: PlaylistOperation):
        """Journal only this episode's edit, retaining a concurrent Stop or newer choice."""
        with self.lock:
            state = self.load()
            if not state or state.get('episode_id') != episode_id:
                raise ValueError('Radio playlist operation belongs to a superseded session')
            state.update(operation_values(operation))
            self._write(json.dumps(state, sort_keys=True))

    def _write(self, encoded):
        # Keep the live transaction journal in RAM during repair. A session
        # created without a local playlist remains transient for its lifetime.
        state = json.loads(encoded)
        if self.paused() or ('local_id' in state and state['local_id'] is None):
            self.transient = encoded
            return
        store = getattr(self.settings,'_store',None)
        with store._lock if store is not None else self.lock:
            if store is not None and radio_storage.available(store.conn):
                self._write_current(store, encoded, state)
            elif encoded != self.last_saved:
                self.settings.set_setting(SESSION_KEY, encoded)
                self.legacy_checkpoint = state
        if encoded != self.last_saved:
            self.last_saved = encoded
        self.transient = None

    def _write_current(self, store, encoded, state):
        with store.conn:
            # Serialize the cutover handoff with other checkpoint writers.
            # Only the untouched import of our observed predecessor may
            # replace its old revision-less token. Never rebase a newer
            # save or Stop, even if it belongs to the same episode.
            if self.revision is None and self.legacy_checkpoint is not None:
                store.conn.execute('UPDATE radio_session_head SET session_id=session_id WHERE 0')
                # Account corrections can change the read projection
                # without another Radio save. Compare the published
                # predecessor, keeping its exact upload and journal data.
                imported,revision = radio_storage.load_published(store.conn)
                if (revision == (self.legacy_checkpoint.get('episode_id'), 1)
                        and imported is not None
                        and radio_checkpoint.normalize(imported) == radio_checkpoint.normalize(self.legacy_checkpoint)):
                    self.revision = revision
            if encoded != self.last_saved or self.transient is not None:
                self.revision = radio_storage.save(store.conn,state,expected=self.revision)
                self.legacy_checkpoint = None
