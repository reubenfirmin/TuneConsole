from typing import Protocol
from ytmusicapi import YTMusic
from yt_playlist.core.config import IdentityConfig
from yt_playlist.core.bridge_session import BridgeSession

class IdentityClient(Protocol):
    def get_library_playlists(self, limit: int = ...) -> list: ...
    def get_playlist(self, playlistId: str, limit: int = ...) -> dict: ...
    def get_history(self) -> list: ...
    def create_playlist(self, title: str, description: str) -> str: ...
    def add_playlist_items(self, playlistId: str, videoIds: list, *, duplicates: bool = False) -> dict: ...
    def remove_playlist_items(self, playlistId: str, videos: list) -> None: ...
    def delete_playlist(self, playlistId: str) -> None: ...
    def search(self, query: str, filter: str = ...) -> list: ...

# ytmusicapi needs an auth blob to build its headers and pass _check_auth for writes. The real auth
# is applied by the extension, so this stub only has to be structurally valid. It carries no secret.
# x-goog-visitor-id is included so construction does not itself trigger a bootstrap GET request to
# fetch a visitor id (ytmusicapi only does that when the header is missing); without it, the first
# frame sent through the bridge would be that bootstrap call rather than the caller's actual request.
STUB_AUTH = {
    "cookie": "SAPISID=stub; __Secure-3PAPISID=stub",
    "authorization": "SAPISIDHASH stub",
    "x-goog-authuser": "0",
    "x-goog-visitor-id": "stub",
    "x-origin": "https://music.youtube.com",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0",
    "accept": "*/*",
}


class BridgeClient(YTMusic):
    def get_history(self):
        from yt_playlist.core.history_snapshot import history_snapshot
        return history_snapshot(self)

    def get_playlist(self, playlistId, limit=100, related=False, suggestions_limit=0):
        if limit is not None or related or suggestions_limit:
            return super().get_playlist(playlistId, limit, related, suggestions_limit)
        from yt_playlist.core.playlist_snapshot import complete_playlist
        return complete_playlist(self, playlistId)

    def get_library_playlists(self, limit=25):
        if limit is not None:
            return super().get_library_playlists(limit=limit)
        from yt_playlist.core.album_snapshot import complete_library_playlists
        return complete_library_playlists(self)

    def get_library_albums(self, limit=25, order=None):
        if limit is not None:
            return super().get_library_albums(limit=limit, order=order)
        from yt_playlist.core.album_snapshot import complete_library_albums
        return complete_library_albums(self, order)

    @property
    def history_timezone(self):
        return self._session._bridge.source_timezone


def build_client(cfg: IdentityConfig, bridge) -> YTMusic:
    # All network I/O is executed by the extension via the bridge session; STUB_AUTH just satisfies
    # ytmusicapi's own construction and auth checks. brand_account_id still selects the identity.
    return BridgeClient(STUB_AUTH, cfg.brand_account_id, requests_session=BridgeSession(bridge))
