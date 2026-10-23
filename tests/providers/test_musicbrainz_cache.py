from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from yt_playlist.providers import musicbrainz


@pytest.fixture(autouse=True)
def artist_cache(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_artist_genre_cache", OrderedDict())
    # Keep cache behavior independent of the enabled vocabulary in other provider tests.
    monkeypatch.setattr(musicbrainz, "_top_label", lambda data: (data.get("genres") or [{}])[0].get("name"))


@pytest.mark.parametrize("initial,ttl", [
    ({"genres": [{"name": "Old"}]}, musicbrainz._ARTIST_TTL_S),
    ({}, musicbrainz._ARTIST_MISS_TTL_S),
])
def test_tagged_and_empty_metadata_refresh_after_their_own_ttl(monkeypatch, initial, ttl):
    clock = [100]
    responses = iter([initial, {"genres": [{"name": "Updated"}]}])
    calls = []

    def get(*args):
        calls.append(args)
        clock[0] += 20  # A slow response does not shorten the retention interval.
        return next(responses)

    monkeypatch.setattr(musicbrainz, "monotonic", lambda: clock[0])
    monkeypatch.setattr(musicbrainz, "_get", get)
    first = musicbrainz._artist_genre("artist")
    clock[0] = 120 + ttl - 1
    assert musicbrainz._artist_genre("artist") == first
    assert len(calls) == 1
    clock[0] += 1
    assert musicbrainz._artist_genre("artist") == "Updated"
    assert len(calls) == 2


def test_eviction_retains_recently_used_artists(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_ARTIST_CACHE_SIZE", 2)
    calls = []

    def get(path, params):
        calls.append(path)
        return {"genres": [{"name": path}]}

    monkeypatch.setattr(musicbrainz, "_get", get)
    for artist in ("a", "b", "a", "c"):
        musicbrainz._artist_genre(artist)
    assert list(musicbrainz._artist_genre_cache) == ["a", "c"]
    assert len(calls) == 3
    musicbrainz._artist_genre("b")
    assert len(calls) == 4 and len(musicbrainz._artist_genre_cache) == 2


def test_transport_failure_is_not_cached_and_can_retry_immediately(monkeypatch):
    calls = []

    def get(*args):
        calls.append(args)
        if len(calls) == 1:
            raise TimeoutError("temporary outage")
        return {"genres": [{"name": "Recovered"}]}

    monkeypatch.setattr(musicbrainz, "_get", get)
    assert musicbrainz._artist_genre("artist") is None
    assert not musicbrainz._artist_genre_cache
    assert musicbrainz._artist_genre("artist") == "Recovered"


def test_overlapping_requests_share_one_refresh(monkeypatch):
    entered, release, second_started = Event(), Event(), Event()
    calls = []

    def get(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return {"genres": [{"name": "Shared"}]}

    def second():
        second_started.set()
        return musicbrainz._artist_genre("artist")

    monkeypatch.setattr(musicbrainz, "_get", get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(musicbrainz._artist_genre, "artist")
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert second_started.wait(5)
        finally:
            release.set()
        assert first.result(5) == other.result(5) == "Shared"
    assert len(calls) == 1
