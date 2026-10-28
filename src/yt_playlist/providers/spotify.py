"""Optional source artwork for import reports; SpotifyIAO owns vendor access."""
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.integrations.spotify import SpotifyIAO

_iao = SpotifyIAO()


def access(store=None):
    return _iao.context(store.enrichment_state if store is not None else None)


def thumbnail(album_url):
    """Return optional public artwork while keeping unavailable access retryable."""
    try:
        return _iao.thumbnail(album_url)
    except IntegrationUnavailable:
        return None


def clear_cache():
    _iao.clear_cache()
