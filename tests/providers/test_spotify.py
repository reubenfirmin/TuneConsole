import io
import json

from yt_playlist.providers import spotify


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_thumbnail_uses_oembed_and_accepts_spotify_cdn(monkeypatch):
    spotify.clear_cache()
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
    spotify.clear_cache()
    monkeypatch.setattr(spotify.urllib.request, "urlopen", lambda *_a, **_k: _Response(json.dumps({
        "thumbnail_url": "https://image-cdn-fa.spotifycdn.com/image/cover123",
    }).encode()))

    assert spotify.thumbnail("https://open.spotify.com/album/abc123") == (
        "https://image-cdn-fa.spotifycdn.com/image/cover123")


def test_thumbnail_rejects_search_urls_and_untrusted_image_hosts(monkeypatch):
    spotify.clear_cache()
    assert spotify.thumbnail("https://open.spotify.com/search/album") is None
    monkeypatch.setattr(spotify.urllib.request, "urlopen", lambda *_a, **_k: _Response(json.dumps({
        "thumbnail_url": "https://evil.example/cover.jpg",
    }).encode()))
    assert spotify.thumbnail("https://open.spotify.com/album/abc123") is None


def test_failure_retries_after_short_delay_and_canonicalizes_cache(monkeypatch):
    spotify.clear_cache()
    clock = [100.0]
    calls = []
    monkeypatch.setattr(spotify.time, "monotonic", lambda: clock[0])

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("offline")
        return "https://i.scdn.co/image/recovered"

    monkeypatch.setattr(spotify, "_fetch_thumbnail", fetch)
    assert spotify.thumbnail("https://open.spotify.com/album/abc123?si=one") is None
    clock[0] += 59
    assert spotify.thumbnail("https://open.spotify.com/album/abc123?si=two") is None
    assert len(calls) == 1
    clock[0] += 1
    assert spotify.thumbnail("https://open.spotify.com/album/abc123").endswith("recovered")
    assert len(calls) == 2


def test_confirmed_misses_expire_and_cache_is_bounded(monkeypatch):
    spotify.clear_cache()
    clock = [0.0]
    calls = []
    monkeypatch.setattr(spotify.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(spotify, "_fetch_thumbnail", lambda url: calls.append(url))
    assert spotify.thumbnail("https://open.spotify.com/album/missing") is None
    clock[0] = spotify._MISS_TTL_S - 1
    assert spotify.thumbnail("https://open.spotify.com/album/missing") is None
    assert len(calls) == 1
    clock[0] += 1
    assert spotify.thumbnail("https://open.spotify.com/album/missing") is None
    assert len(calls) == 2
    for n in range(spotify._CACHE_LIMIT + 5):
        spotify.thumbnail(f"https://open.spotify.com/album/a{n}")
    assert len(spotify._cache) == spotify._CACHE_LIMIT
    spotify.clear_cache()
