"""Caller caches must preserve retry evidence and retained Last.fm metadata."""
import io
import json
import urllib.error
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.integrations.lastfm import LastFmIAO, POSITIVE_TTL
from yt_playlist.providers import lastfm
from yt_playlist.rec import discover, road_trip_candidates as candidates
from yt_playlist.util import net


@pytest.fixture
def provider(store, monkeypatch):
    clock, calls = [1000.0], []
    monkeypatch.delenv('LASTFM_API_KEY', raising=False)
    store.settings.set_setting('lastfm_api_key', 'controlled-key')
    for cache in (candidates._ARTIST_GENRE_CACHE, candidates._GENRE_ARTIST_CACHE):
        cache.clear()
    monkeypatch.setattr(candidates, 'monotonic', lambda: clock[0])

    def install(fetch):
        def request(req, **kwargs):
            calls.append(req)
            return io.BytesIO(json.dumps(fetch()).encode())
        iao = LastFmIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
                        open_fn=request, pacer=SimpleNamespace(wait=lambda: None))
        monkeypatch.setattr(lastfm, '_iao', iao)
        return SimpleNamespace(clock=clock, calls=calls)
    yield install
    for cache in (candidates._ARTIST_GENRE_CACHE, candidates._GENRE_ARTIST_CACHE):
        cache.clear()


def outage():
    raise urllib.error.HTTPError('https://ws.audioscrobbler.com/2.0/', 503, 'controlled outage', {}, None)


def test_artist_metadata_keeps_last_good_value_and_pending_state_on_cache_hits(provider, store):
    def fetch():
        if len(p.calls) == 2:
            outage()
        return {'toptags': {'tag': [{'name': 'house' if len(p.calls) == 1 else 'folk'}]}}
    p = provider(fetch)
    assert candidates.artist_genre(store, 'Artist') == 'House'
    p.clock[0] += POSITIVE_TTL
    with net.probe_errors() as failed:
        assert candidates.artist_genre(store, 'Artist') == 'House'
    with net.probe_errors() as cached:
        assert candidates.artist_genre(store, 'Artist') == 'House'
    assert len(p.calls) == 2
    assert len(failed) == len(cached) == 1
    assert cached[0].provider == 'lastfm' and cached[0].retry_at == failed[0].retry_at
    assert cached[0].__traceback__ is None
    p.clock[0] += candidates._METADATA_RETRY_TTL
    with net.probe_errors() as recovered:
        assert candidates.artist_genre(store, 'Artist') == 'Folk'
    assert not recovered and len(p.calls) == 3


def test_genre_fallback_remains_usable_without_caching_failure_for_a_day(provider, store, monkeypatch):
    def fetch():
        if len(p.calls) == 1:
            outage()
        return {'topartists': {'artist': [{'name': 'Recovered'}]}}
    p = provider(fetch)
    monkeypatch.setattr(candidates.musicbrainz, 'tag_artists', lambda *args, **kwargs: ['Fallback'])
    with net.probe_errors() as failed:
        assert candidates.genre_artists(store, 'Rock') == ['Fallback']
    with net.probe_errors() as pending:
        assert candidates.genre_artists(store, 'Rock') == ['Fallback']
    assert failed and pending and len(p.calls) == 1
    p.clock[0] += candidates._METADATA_RETRY_TTL
    with net.probe_errors() as recovered:
        assert candidates.genre_artists(store, 'Rock') == ['Recovered']
    assert not recovered and len(p.calls) == 2


def discovery_context(store, clock, monkeypatch):
    # Exercise the real similarity repository without fitting unrelated models
    # or making image/YouTube requests. No ranked candidates are needed here.
    monkeypatch.setattr(discover, 'playlist_taste', lambda store: SimpleNamespace())
    monkeypatch.setattr(discover.vectors, 'load_vectors', lambda store: (['core'], np.ones((1, 1)), {'core': 0}))
    monkeypatch.setattr(discover, '_anchors', lambda *args, **kwargs: [('CoreBand', 1, np.ones(1))])
    monkeypatch.setattr(discover.artist_model, 'artist_neighbors', lambda *args, **kwargs: [])
    return SimpleNamespace(store=store, now_fn=lambda: clock[0])


def test_failed_similarity_refresh_keeps_the_graph_and_original_fetch_time(provider, store, monkeypatch):
    def fetch():
        if len(p.calls) == 2:
            outage()
        return {'similarartists': {'artist': [{'name': 'Fresh' if len(p.calls) == 1 else 'Updated',
                                               'match': '0.9'}]}}
    p = provider(fetch)
    ctx = discovery_context(store, p.clock, monkeypatch)
    discover.new_artists(ctx)
    assert store.artist_similarity.cached_similar('CoreBand', p.clock[0]) == [['Fresh', 0.9]]
    p.clock[0] += 14 * 86400 + 1
    with net.probe_errors() as failed:
        discover.new_artists(ctx)
    assert failed and len(p.calls) == 2
    assert store.artist_similarity.cached_similar('CoreBand', p.clock[0]) is None
    assert store.artist_similarity.cached_similar('CoreBand', p.clock[0], ttl_days=float('inf')) == [['Fresh', 0.9]]
    assert store.artist_similarity.artist_similar_edges('CoreBand')
    p.clock[0] += 30
    with net.probe_errors() as recovered:
        discover.new_artists(ctx)
    assert not recovered and len(p.calls) == 3
    assert store.artist_similarity.cached_similar('CoreBand', p.clock[0]) == [['Updated', 0.9]]


def test_unavailable_similarity_is_not_stored_as_a_confirmed_empty_result(provider, store, monkeypatch):
    def fetch():
        if len(p.calls) == 1:
            outage()
        return {'similarartists': {'artist': []}}
    p = provider(fetch)
    ctx = discovery_context(store, p.clock, monkeypatch)
    discover.new_artists(ctx)
    assert store.artist_similarity.cached_similar('CoreBand', p.clock[0]) is None
    p.clock[0] += 30
    discover.new_artists(ctx)
    assert store.artist_similarity.cached_similar('CoreBand', p.clock[0]) == []
    p.clock[0] += 2 * 86400
    discover.new_artists(ctx)
    assert len(p.calls) == 2
