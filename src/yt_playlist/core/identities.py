from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicError
from yt_playlist.core.bridge import BridgeError
from yt_playlist.core.config import AccountConfig
from yt_playlist.core.bridge_session import BridgeSession

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
    def deletion_read_report(self, playlist_id, expected_brand):
        """Report request scope and provider authentication without returning an account profile."""
        from yt_playlist.core.playlist_snapshot import playlist_read_report

        bridge = self._session._bridge
        connection = getattr(bridge, 'connection_id', None)
        delegated = self.context['context']['user'].get('onBehalfOfUser') or None
        scope = {'request_account': 'delegated' if delegated else 'browser_default',
                 'configured_account_matches': delegated == (expected_brand or None),
                 'account_menu': 'not_checked'}
        if not scope['configured_account_matches']:
            return {'scope': scope}
        try:
            account = self.get_account_info()
        except (BridgeError, YTMusicError, KeyError, IndexError, TypeError, ValueError, OSError):
            scope['account_menu'] = 'unverified'
        else:
            scope['account_menu'] = ('authenticated' if isinstance(account, dict) and account.get('accountName')
                                     else 'unverified')
        reads = playlist_read_report(self, playlist_id)
        scope['bridge_connection_unchanged'] = (connection is not None
                                                and connection == getattr(bridge, 'connection_id', None))
        # A valid menu proves authentication for the request context. It does
        # not prove ownership of a target that returned no matching header.
        return {'scope': scope, **reads}

    def get_video_storyboard(self, video_id):
        # get_song intentionally strips storyboards from the player response.
        # Keep this metadata-only read on the same authenticated browser bridge.
        response = self._send_request('player', {'videoId': video_id})
        if (response.get('videoDetails') or {}).get('videoId') != video_id:
            return {}
        return response.get('storyboards') or {}

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


def build_client(cfg: AccountConfig, bridge) -> YTMusic:
    # All network I/O is executed by the extension via the bridge session; STUB_AUTH just satisfies
    # ytmusicapi's own construction and auth checks. brand_account_id still selects the identity.
    return BridgeClient(STUB_AUTH, cfg.brand_account_id,
                        requests_session=BridgeSession(bridge, identity=cfg.brand_account_id))
