"""One active playlist operation and one queued direction, with explicit ownership."""
from dataclasses import dataclass, field
from typing import TypedDict

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


@dataclass
class RadioEdits:
    active: PlaylistOperation | None = None
    requested: DirectionRequest | None = None
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
