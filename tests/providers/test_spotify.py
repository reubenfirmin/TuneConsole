import io
import json

from yt_playlist.providers import spotify


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_thumbnail_uses_oembed_and_accepts_spotify_cdn(monkeypatch):
    spotify.thumbnail.cache_clear()
    seen = []

    def open_(request, timeout):
        seen.append((request.full_url, timeout))
        return _Response(json.dumps({
            "thumbnail_url": "https://i.scdn.co/image/cover123",
        }).encode())

    monkeypatch.setattr(spotify.urllib.request, "urlopen", open_)
    result = spotify.thumbnail("https://open.spotify.com/album/abc123")

    assert result == "https://i.scdn.co/image/cover123"
    assert "url=https%3A%2F%2Fopen.spotify.com%2Falbum%2Fabc123" in seen[0][0]


def test_thumbnail_accepts_spotifys_current_image_cdn(monkeypatch):
    spotify.thumbnail.cache_clear()
    monkeypatch.setattr(spotify.urllib.request, "urlopen", lambda *_a, **_k: _Response(json.dumps({
        "thumbnail_url": "https://image-cdn-fa.spotifycdn.com/image/cover123",
    }).encode()))

    assert spotify.thumbnail("https://open.spotify.com/album/abc123") == (
        "https://image-cdn-fa.spotifycdn.com/image/cover123")


def test_thumbnail_rejects_search_urls_and_untrusted_image_hosts(monkeypatch):
    spotify.thumbnail.cache_clear()
    assert spotify.thumbnail("https://open.spotify.com/search/album") is None
    monkeypatch.setattr(spotify.urllib.request, "urlopen", lambda *_a, **_k: _Response(json.dumps({
        "thumbnail_url": "https://evil.example/cover.jpg",
    }).encode()))
    assert spotify.thumbnail("https://open.spotify.com/album/abc123") is None
