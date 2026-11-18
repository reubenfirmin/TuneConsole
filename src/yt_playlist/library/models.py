"""Detached library memberships shared by policies, queries and persistence."""
from dataclasses import dataclass


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
        return key(self.account_id, self.provider_playlist_id)


@dataclass
class PlaylistMemberships:
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
