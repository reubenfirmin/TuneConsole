"""One active playlist operation and one queued direction, with explicit ownership."""
from dataclasses import dataclass, field
from typing import NotRequired, TypedDict

from yt_playlist.web.radio_playlist_io import PendingWrite
from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail


@dataclass
class AppendOperation:
    track: dict
    write: PendingWrite | None = None


PlaylistOperation = AppendOperation | RadioPlaylistTail


class DirectionRequest(TypedDict):
    slot: int
    tracks: list[dict]
    choice: dict | None
    play: NotRequired[dict]
    excluded: NotRequired[list[str]]
    resequence_after: NotRequired[str]


@dataclass(frozen=True)
class SongContinuation:
    video_id: str
    choice: dict | None
    excluded: tuple[str, ...]
    from_video_id: str | None = None


@dataclass
class RadioEdits:
    active: PlaylistOperation | None = None
    requested: DirectionRequest | None = None
    handoff: SongContinuation | None = None
    windows: list = field(default_factory=list)
    recent_states: list = field(default_factory=list)
    conflict: tuple | None = None
    conflict_at: float = 0

    @property
    def append(self):
        return self.active if isinstance(self.active, AppendOperation) else None

    @property
    def replacement(self):
        return self.active if isinstance(self.active, RadioPlaylistTail) else None

    def song_continuation(self):
        """Find the selected song in the saved edit or the live playback handoff."""
        if request := self.requested:
            video = (request.get('play') or {}).get('video_id') or request.get('resequence_after')
            if video:
                return SongContinuation(video, request['choice'], tuple(request.get('excluded', ())))
        elif tail := self.replacement:
            video = (tail.play or {}).get('video_id') or tail.resequence_after
            if video:
                return SongContinuation(video, tail.choice_id, tuple(tail.excluded))
        return self.handoff

    def observe_song(self, video):
        """Release the handoff once playback starts the chosen song or moves elsewhere."""
        if self.handoff and (video == self.handoff.video_id or video != self.handoff.from_video_id):
            self.handoff = None

    def previously_saved(self, video):
        # The open replacement outlives the short readback buffer. A pending
        # write alone does not establish that its new song entered the queue.
        if tail := self.replacement:
            if any(video in ids and (ids != tail.waiting or ids == tail.confirmed) for ids in tail.allowed):
                return True
        return any(video in state['ids'] for state in self.recent_states)

    def begin(self, operation: PlaylistOperation):
        if self.active is not None:
            raise ValueError('Resolve the active playlist operation before beginning another')
        self.active = operation

    def finish(self, operation: PlaylistOperation):
        if operation is not self.active:
            raise ValueError('Cannot finish a superseded playlist operation')
        self.active = None
        self.conflict = None

    def queue(self, request: DirectionRequest):
        # A later DJ choice supersedes an unsent choice, never an in-flight write.
        self.requested = request
