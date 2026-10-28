import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from yt_playlist.providers import musicbrainz
from yt_playlist.integrations import musicbrainz as access
from yt_playlist.util import net


@pytest.fixture
def artist_cache(monkeypatch):
    def install(fetch):
        clock = [100.0]
        def open_request(request, **kwargs):
            from urllib.parse import urlsplit
            identity = urlsplit(request.full_url).path.rsplit('/', 1)[1]
            return io.StringIO(json.dumps({'id': identity, 'name': 'Artist', **fetch(request, clock)}))
        iao = access.MusicBrainzIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=lambda: None))
        monkeypatch.setattr(musicbrainz, '_iao', iao)
        monkeypatch.setattr(musicbrainz, '_top_label',
                            lambda data: (data.get('genres') or [{}])[0].get('name'))
        return iao, clock
    return install


@pytest.mark.parametrize("initial,ttl", [
    ({"genres": [{"name": "Old"}]}, access.POSITIVE_TTL),
    ({'genres': [], 'tags': []}, access.MISS_TTL),
])
def test_tagged_and_empty_metadata_refresh_after_their_own_ttl(artist_cache, initial, ttl):
    responses = iter([initial, {"genres": [{"name": "Updated"}]}])
    calls = []

    def get(request, clock):
        calls.append(request.full_url)
        clock[0] += 20  # A slow response does not shorten the retention interval.
        return next(responses)

    iao, clock = artist_cache(get)
    first = musicbrainz._artist_genre("artist")
    clock[0] = 120 + ttl - 1
    assert musicbrainz._artist_genre("artist") == first
    assert len(calls) == 1
    clock[0] += 1
    assert musicbrainz._artist_genre("artist") == "Updated"
    assert len(calls) == 2


def test_eviction_retains_recently_used_artists(monkeypatch, artist_cache):
    monkeypatch.setattr(access, "CACHE_SIZE", 2)
    calls = []

    def get(request, clock):
        calls.append(request.full_url)
        return {"genres": [{"name": request.full_url}]}

    artist_cache(get)
    for artist in ("a", "b", "a", "c", "a", "b"):
        musicbrainz._artist_genre(artist)
    assert len(calls) == 4
    assert calls[-1].split('?')[0].endswith('/artist/b')


def test_transport_failure_is_deferred_without_negative_caching(artist_cache):
    calls = []

    def get(request, clock):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise TimeoutError("temporary outage")
        return {"genres": [{"name": "Recovered"}]}

    iao, clock = artist_cache(get)
    with net.probe_errors() as errors:
        assert musicbrainz._artist_genre("artist") is None
    assert len(errors) == 1
    assert musicbrainz._artist_genre("artist") is None
    assert len(calls) == 1
    clock[0] = errors[0].retry_at
    assert musicbrainz._artist_genre("artist") == "Recovered"
    assert len(calls) == 2


def test_failed_refresh_retains_last_good_artist_and_reports_retry(artist_cache):
    calls = []
    def get(request, clock):
        calls.append(request.full_url)
        if len(calls) == 2:
            raise TimeoutError('temporary outage')
        return {'genres': [{'name': 'Old' if len(calls) == 1 else 'Updated'}]}
    iao, clock = artist_cache(get)
    assert musicbrainz._artist_genre('artist') == 'Old'
    clock[0] += access.POSITIVE_TTL
    with net.probe_errors() as errors:
        assert musicbrainz._artist_genre('artist') == 'Old'
    assert errors and errors[0].provider == 'musicbrainz'
    assert musicbrainz._artist_genre('artist') == 'Old'
    assert len(calls) == 2
    clock[0] = errors[0].retry_at
    assert musicbrainz._artist_genre('artist') == 'Updated'
    assert len(calls) == 3


def test_overlapping_requests_share_one_refresh(artist_cache):
    entered, release, second_started = Event(), Event(), Event()
    calls = []

    def get(request, clock):
        calls.append(request.full_url)
        entered.set()
        assert release.wait(5)
        return {"genres": [{"name": "Shared"}]}

    def second():
        second_started.set()
        return musicbrainz._artist_genre("artist")

    artist_cache(get)
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
