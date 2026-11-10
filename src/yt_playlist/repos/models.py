"""Row dataclasses shared by the Store and its DAOs.

Kept in their own module so DAOs can return typed rows without importing Store (which would create
a Store/repository import cycle). Consumers import these row types directly.
"""
from dataclasses import dataclass


@dataclass
class Account:
    id: int; label: str; credential_ref: str
    brand_account_id: str | None; is_master: bool; last_auth_ok: float | None


@dataclass
class Playlist:
    id: int; account_id: int; provider_playlist_id: str; title: str
    track_count: int; content_hash: str
    first_seen: float; last_seen: float; last_changed: float
    thumbnail: str | None = None
    created_at: float | None = None
    created_at_source: str | None = None

    @property
    def playlist_scope_id(self):
        from yt_playlist.core.playlist_scopes import key
        return key(self.account_id,self.provider_playlist_id)


@dataclass
class PlaylistMemberships:
    """A detached read model; recording keys and upload kinds share one snapshot."""
    playlists: list[Playlist]
    keys: dict[int, frozenset[str]]
    kinds: dict[str, str]
    excluded: set[int]


@dataclass
class CleanupPreferences:
    ignored: dict[str, set[str]]
    suppressed: list[tuple[str, str, float]]
    ignored_ytm: set[str]
    kept: set[frozenset[str]]
    merges: list[dict]


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
