"""OAuth token lifecycle for the local Spotify import connection."""
import requests

from yt_playlist.providers import spotify


class SpotifyDisconnected(RuntimeError):
    pass


def import_client(store, now, session=None):
    """Return an authenticated transient client, refreshing shortly-expiring access as needed."""
    client_id = store.get_setting("spotify_client_id")
    access = store.get_setting("spotify_access_token")
    refresh = store.get_setting("spotify_refresh_token")
    if not client_id or not refresh:
        raise SpotifyDisconnected("Spotify is not connected")
    try:
        expires = float(store.get_setting("spotify_access_expires_at", "0"))
    except (TypeError, ValueError):
        expires = 0
    http = session or requests.Session()
    if not access or expires <= now + 30:
        token = spotify.refresh_access_token(http, client_id, refresh)
        access = token["access_token"]
        store.set_setting("spotify_access_token", access)
        store.set_setting("spotify_access_expires_at", str(now + int(token.get("expires_in", 3600))))
        if token.get("refresh_token"):  # rotation is optional, but never discard one when supplied
            store.set_setting("spotify_refresh_token", token["refresh_token"])
    return spotify.SpotifyImportClient(access, session=http)
