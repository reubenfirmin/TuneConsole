"""Radio lifecycle and session data, independent of transport and persistence."""
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_policy import CandidateView, FeedbackContext, FeedbackUpdate, PlaybackHistory
from yt_playlist.web.radio_edits import RadioEdits
from yt_playlist.web.radio_playlist_tail import TailView


class Phase(StrEnum):
    PREPARING = "preparing"
    AWAITING_PLAYBACK = "awaiting_playback"
    PLAYING = "playing"
    APPENDING = "appending"
    REPLACING = "replacing"
    CONFLICT = "conflict"
    STOPPING = "stopping"
    STOPPED = "stopped"


_RUNNING = {Phase.AWAITING_PLAYBACK, Phase.PLAYING, Phase.APPENDING, Phase.REPLACING}
_TRANSITIONS = {
    Phase.PREPARING: {Phase.AWAITING_PLAYBACK, Phase.CONFLICT, Phase.STOPPING},
    **{phase: (_RUNNING - {phase}) | {Phase.CONFLICT, Phase.STOPPING} for phase in _RUNNING},
    Phase.CONFLICT: _RUNNING | {Phase.PREPARING, Phase.STOPPING},
    Phase.STOPPING: {Phase.STOPPED},
    Phase.STOPPED: set(),
}


@dataclass
class Session:
    client: object
    candidates: RadioCandidates
    identity: object
    title: str
    phase: Phase = Phase.PREPARING
    stop: threading.Event = field(default_factory=threading.Event)
    selected: list = field(default_factory=list)
    playlist: str | None = None
    local_id: int | None = None
    tracks: list = field(default_factory=list)
    position: int = -1
    furthest: int = -1
    edits: RadioEdits = field(default_factory=RadioEdits)
    initial_tracks: list = field(default_factory=list)
    error: str | None = None
    exhausted: bool = False
    browser: dict | None = None
    browser_seen_at: float | None = None
    refresh_needed: bool = False
    directions: RadioDirections | None = None
    episode_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = 0
    transition: int = 0
    observed_video: str | None = None
    starts: dict = field(default_factory=dict)
    feedback_seen: set = field(default_factory=set)
    seen_menus: set = field(default_factory=set)
    choices: dict = field(default_factory=dict)
    heard: list = field(default_factory=list)
    rejected: set = field(default_factory=set)
    monitoring: bool = False
    feedback_dirty: bool = False
    observer_error: str | None = None
    observer_report: tuple | None = None
    launch_deadline: float = 0
    preparation_deadline: float = 0
    preparation_snapshot: tuple[str, ...] | None = None
    preparation_seen_at: float = 0
    playback_confirmed: bool = False
    metadata_checked_at: float = 0
    metadata_revision: int = 0
    metadata_rejected: set = field(default_factory=set)
    candidates_checked_at: float = 0
    preparation_requested: bool = False
    now_refreshing: bool = False
    now_requested_at: float | None = None
    skip_requested: bool = False

    @property
    def pending(self):
        return self.edits.append.track if self.edits.append else None

    @property
    def replacement(self):
        return self.edits.replacement

    @property
    def requested_direction(self):
        return self.edits.requested

    def candidate_view(self):
        return CandidateView(self.candidates, self.directions, tuple(self.selected),
                             frozenset(self.rejected), frozenset(self.metadata_rejected))

    def history_view(self):
        return PlaybackHistory(tuple(self.tracks), tuple(self.heard), self.position, self.furthest, self.transition)

    def feedback_context(self):
        return FeedbackContext(self.playlist, self.identity, self.started_at, dict(self.starts),
                               self.observed_video, tuple(tuple(window) for window in self.edits.windows),
                               frozenset(self.feedback_seen))

    def accept_feedback(self, update: FeedbackUpdate):
        self.directions.blocked = set(update.blocked)
        self.directions.feedback = update.strengths
        self.feedback_seen = set(update.seen)
        self.feedback_dirty = self.feedback_dirty or update.changed

    def accept_tracks(self, tracks, now):
        """Apply a confirmed queue under the caller's state lock, preserving heard positions."""
        if [t['video_id'] for t in tracks] == [t['video_id'] for t in self.tracks]:
            if list(tracks) != self.tracks:
                self.tracks = list(tracks)
                by_video = {t['video_id']: t for t in tracks}
                self.selected = [by_video.get(t['video_id'], t) for t in self.selected]
                self.metadata_revision += 1
            return
        self.edits.recent_states = [state for state in self.edits.recent_states if now - state['at'] < 60]
        self.edits.recent_states.append({'ids': [t['video_id'] for t in self.tracks], 'at': now})
        current = self.tracks[self.position]['video_id'] if self.position >= 0 else None
        past = {t['video_id'] for t in self.tracks[:self.furthest + 1]}
        self.position = next((i for i, t in enumerate(tracks) if t['video_id'] == current), -1)
        self.furthest = max((i for i, t in enumerate(tracks) if t['video_id'] in past), default=-1)
        self.tracks = list(tracks)
        self.selected = list(tracks)

    def accept_mutation(self, result, now):
        """Apply transaction results and transfer choice credit to an accepted upload."""
        for substitution in result.substitutions:
            requested, accepted = substitution.requested, substitution.accepted
            self.candidates.retain([accepted])
            self.rejected.add(requested['video_id'])
            if self.directions:
                self.directions.blocked.add(requested['video_id'])
            choice = self.choices.pop(requested['video_id'], None)
            if choice:
                self.choices[accepted['video_id']] = choice
        if result.tracks is not None:
            self.accept_tracks(result.tracks, now)

    def playlist_view(self, now):
        self.edits.recent_states = [state for state in self.edits.recent_states if now - state['at'] < 60]
        return TailView(tuple(self.tracks), self.position, self.furthest,
                        frozenset(self.directions.blocked) if self.directions else frozenset(),
                        self.candidates.by_video,
                        tuple(tuple(state['ids']) for state in self.edits.recent_states), self.skip_requested)

    @property
    def starting(self):
        return self.phase == Phase.PREPARING

    @property
    def halted(self):
        return self.phase == Phase.CONFLICT

    @property
    def stopped_by_user(self):
        return self.phase in {Phase.STOPPING, Phase.STOPPED}

    def move(self, phase: Phase):
        if phase == self.phase:
            return
        if phase not in _TRANSITIONS[self.phase]:
            raise ValueError(f"Illegal radio transition: {self.phase} to {phase}")
        self.phase = phase

    def running_phase(self):
        """The outstanding transaction takes precedence over a queued choice."""
        if self.initial_tracks:
            return Phase.PREPARING
        if self.pending:
            return Phase.APPENDING
        if self.replacement or self.requested_direction:
            return Phase.REPLACING
        return Phase.PLAYING if self.playback_confirmed else Phase.AWAITING_PLAYBACK

    def settle(self):
        if not self.halted and not self.stopped_by_user:
            self.move(self.running_phase())

    def request_stop(self):
        if not self.stopped_by_user:
            self.move(Phase.STOPPING)
        self.stop.set()


def selected_slot(s: Session):
    if s.requested_direction:
        return s.requested_direction["slot"]
    if s.replacement:
        return s.replacement.slot
    return s.directions.active if s.directions else None
