"""Row dataclasses shared by the Store and its DAOs.

Kept in their own module so DAOs can return typed rows without importing Store (which would create
a Store/repository import cycle). Consumers import these row types directly.
"""
from dataclasses import dataclass
from yt_playlist.library.models import Playlist, PlaylistMemberships, CleanupPreferences


@dataclass
class Account:
    id: int
    label: str
    brand_account_id: str | None
    is_master: bool


@dataclass
class Track:
    id: int; video_id: str | None; title: str; artist: str
    album: str | None; duration_s: int | None; identity_key: str


@dataclass
class Action:
    id: int; kind: str; params_json: str | None; plan_json: str | None; undo_json: str | None
    status: str; created_at: float; executed_at: float | None
    revision: str | None = None
    execution_token: str | None = None
