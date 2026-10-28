"""Controlled transport checks for the shared Last.fm boundary; no vendor calls."""
import io
import json
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from threading import Event
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations import lastfm as access
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.providers import lastfm
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.util import net


def tags(name='house'):
    return {'toptags': {'tag': [{'name': name}] if name else []}}


def query(method='artist.gettoptags', **kwargs):
    return {'method': method, 'artist': 'Artist', 'api_key': 'secret', **kwargs}


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.delenv('LASTFM_API_KEY', raising=False)
    def install(fetch):
        clock, calls, responses, paces = [1000.0], [], [], []

        def open_request(request, *, timeout):
            calls.append((request, timeout))
            body = fetch(request)
            response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
            responses.append(response)
            return response

        iao = access.LastFmIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=lambda: paces.append(clock[0])))
        monkeypatch.setattr(lastfm, '_iao', iao)
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, paces=paces)
    return install


def http(code, headers=None):
    return urllib.error.HTTPError('https://ws.audioscrobbler.com/2.0/', code,
                                  'controlled error', headers or {}, None)


@pytest.mark.parametrize('body', [tags(), tags(None)])
def test_response_cache_is_shared_detached_and_expires_after_completion(boundary, body):
    def fetch(request):
        b.clock[0] += 20
        return body
    b = boundary(fetch)
    result = b.iao.get(query())
    result.clear()
    ttl = access.POSITIVE_TTL if body['toptags']['tag'] else access.MISS_TTL
    b.clock[0] = 1020 + ttl - 1
    assert b.iao.get(query()) == body
    assert len(b.calls) == 1
    b.clock[0] += 1
    assert b.iao.get(query()) == body
    assert len(b.calls) == len(b.paces) == 2
    assert all(response.closed for response in b.responses)
    request, timeout = b.calls[0]
    assert timeout == access.TIMEOUT_S
    assert request.get_header('User-agent') == access.USER_AGENT
    assert request.get_header('Accept') == 'application/json'


@pytest.mark.parametrize('failure', [TimeoutError(), http(503), {'error': 11}, {'error': 29}])
def test_one_api_outage_defers_other_entry_points_and_recovers(boundary, store, failure):
    def fetch(request):
        if len(b.calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return tags()
    b = boundary(fetch)
    store.settings.set_setting('lastfm_api_key', 'secret')
    with lastfm.access(store), net.probe_errors() as errors:
        assert lastfm.artist_genre('Artist', 'secret') is None
        assert lastfm.similar_artists('Other', 'secret') == []
        assert lastfm.tag_top_artists('Rock', 'secret') == []
        assert lastfm.probe({'title': 'Song', 'artist': 'Artist'}, store, key='secret').fields == {}
    assert len(b.calls) == 1
    assert len(errors) == 4
    assert all(error.provider == 'lastfm' and error.retry_at == 1030 for error in errors)
    assert store.enrichment_state.integration_cooldown('lastfm')['retry_at'] == 1030
    lastfm.reset()
    assert lastfm.tripped()  # Batch boundaries cannot clear shared availability.
    b.clock[0] = 1030
    with lastfm.access(store):
        assert lastfm.artist_genre('Artist', 'secret') == 'House'
    assert len(b.calls) == 2
    assert store.enrichment_state.integration_cooldown('lastfm') is None
    assert not lastfm.tripped()


@pytest.mark.parametrize('header,expected', [
    ('120', 1120),
    (format_datetime(datetime.fromtimestamp(1600, tz=timezone.utc), usegmt=True), 1600),
    ('invalid', 1030), ('', 1030), ('-5', 1030),
])
def test_retry_after_survives_a_new_access_object(boundary, store, header, expected):
    def fail(request):
        raise http(429, {'Retry-After': header})
    b = boundary(fail)
    store.settings.set_setting('lastfm_api_key', 'secret')
    with lastfm.access(store), pytest.raises(IntegrationUnavailable) as first:
        b.iao.get(query())
    assert first.value.retry_at == expected
    recreated = access.LastFmIAO(now_fn=lambda: b.clock[0],
        open_fn=lambda *args, **kwargs: pytest.fail('Persisted wait was bypassed'),
        pacer=SimpleNamespace(wait=lambda: pytest.fail('Deferred requests should not pace')))
    with recreated.context(store), pytest.raises(IntegrationUnavailable) as deferred:
        recreated.get(query('artist.getsimilar'))
    assert deferred.value.retry_at == expected


def test_http_200_rate_limit_also_honors_retry_after(boundary):
    b = boundary(lambda request: {'error': 29})
    original = b.iao.open

    def request(req, **kwargs):
        response = original(req, **kwargs)
        response.headers = {'Retry-After': '120'}
        return response
    b.iao.open = request
    with pytest.raises(IntegrationUnavailable) as caught:
        b.iao.get(query())
    assert caught.value.retry_at == 1120


@pytest.mark.parametrize('failure', [{'error': 10}, {'error': 26}, http(403)])
def test_bad_key_is_shared_across_methods_but_new_key_is_admitted(boundary, store, failure):
    def fetch(request):
        if len(b.calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return tags()
    b = boundary(fetch)
    store.settings.set_setting('lastfm_api_key', 'secret')
    with lastfm.access(store), net.probe_errors() as errors:
        assert lastfm.artist_genre('Artist', 'secret') is None
        assert lastfm.similar_artists('Other', 'secret') == []
        store.settings.set_setting('lastfm_api_key', 'replacement')
        assert lastfm.artist_genre('Artist', 'secret') == 'House'
    assert len(b.calls) == 2
    assert len(errors) == 2 and not any(net.provider_outage(error) for error in errors)
    assert store.enrichment_state.integration_cooldown('lastfm') is None
    assert 'secret' not in repr(b.iao._cache) + repr(b.iao._waits)


def test_store_key_change_and_removal_are_resolved_each_time(boundary, store, monkeypatch, tmp_path):
    monkeypatch.delenv('LASTFM_API_KEY', raising=False)
    monkeypatch.setattr(access.credentials.paths, 'config_path', lambda: tmp_path / 'missing.toml')
    b = boundary(lambda request: tags())
    with b.iao.context(store):
        with pytest.raises(IntegrationUnavailable):
            b.iao.get({'method': 'artist.gettoptags', 'artist': 'Artist'})
        for key in ('first', 'second'):
            store.settings.set_setting('lastfm_api_key', key)
            assert lastfm.api_key() == key
            assert b.iao.get({'method': 'artist.gettoptags', 'artist': 'Artist'}) == tags()
        store.settings.set_setting('lastfm_api_key', '')
        with pytest.raises(IntegrationUnavailable):
            b.iao.get({'method': 'artist.gettoptags', 'artist': 'Artist', 'api_key': 'first'})
    assert len(b.calls) == 2


def test_key_edit_during_pacing_is_seen_before_http(boundary, store):
    b = boundary(lambda request: tags())
    store.settings.set_setting('lastfm_api_key', 'first')

    def wait():
        store.settings.set_setting('lastfm_api_key', 'second')
    b.iao.pacer.wait = wait
    with lastfm.access(store):
        assert lastfm.artist_genre('Artist', 'first') == 'House'
    assert len(b.calls) == 1
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(b.calls[0][0].full_url).query)
    assert params['api_key'] == ['second']


def test_last_good_response_survives_failed_refresh_and_recovery(boundary):
    def fetch(request):
        if len(b.calls) == 2:
            raise TimeoutError()
        return tags('house' if len(b.calls) == 1 else 'folk')
    b = boundary(fetch)
    assert lastfm.artist_genre('Artist', 'secret') == 'House'
    b.clock[0] += access.POSITIVE_TTL
    with net.probe_errors() as errors:
        assert lastfm.artist_genre('Artist', 'secret') == 'House'
    assert len(errors) == 1
    with net.probe_errors() as pending:
        assert lastfm.artist_genre('Artist', 'secret') == 'House'
    assert pending and len(b.calls) == 2
    b.clock[0] = errors[0].retry_at
    with net.probe_errors() as recovered:
        assert lastfm.artist_genre('Artist', 'secret') == 'Folk'
    assert not recovered and len(b.calls) == 3


@pytest.mark.parametrize('body,missing', [
    ({'error': 6, 'message': 'Track not found'}, True),
    ({'error': 6, 'message': 'Invalid parameters'}, False),
    ({'error': 6}, False), ({'unexpected': []}, False), ([], False), (b'invalid json', False),
])
def test_only_confirmed_misses_enter_negative_cache(boundary, body, missing):
    b = boundary(lambda request: body)
    params = query('track.getinfo', track='Song')
    with net.probe_errors() as errors:
        if missing:
            assert b.iao.get(params) == body
        else:
            with pytest.raises(IntegrationUnavailable):
                b.iao.get(params)
    assert bool(errors) != missing
    assert bool(b.iao._cache['api']) == missing


def test_query_failure_allows_other_healthy_api_work(boundary):
    def fetch(request):
        method = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)['method'][0]
        return {'error': 6, 'message': 'Invalid parameters'} if method == 'track.getinfo' else tags()
    b = boundary(fetch)
    with pytest.raises(IntegrationUnavailable):
        b.iao.get(query('track.getinfo', track='Song'))
    assert lastfm.artist_genre('Artist', 'secret') == 'House'
    assert not lastfm.tripped()
    assert len(b.calls) == 2


@pytest.mark.parametrize('header,code', [({'Retry-After': '120'}, 503), ({}, 429)])
def test_page_throttling_is_shared_while_the_api_stays_healthy(boundary, store, header, code):
    def fetch(request):
        if 'last.fm' in urllib.parse.urlsplit(request.full_url).netloc:
            raise http(code, header)
        return tags()
    b = boundary(fetch)
    store.settings.set_setting('lastfm_api_key', 'secret')
    with lastfm.access(store), net.probe_errors() as errors:
        for album in ('A', 'B'):
            with pytest.raises(IntegrationUnavailable):
                lastfm._fetch_text(f'https://www.last.fm/music/Artist/{album}')
        assert lastfm.artist_genre('Artist', 'secret') == 'House'
    assert len(b.calls) == 2 and len(b.paces) == 2
    assert len(errors) == 2 and all(error.provider == 'lastfm:pages' for error in errors)
    assert store.enrichment_state.integration_cooldown('lastfm') is None
    assert store.enrichment_state.integration_cooldown('lastfm:pages') is not None


@pytest.mark.parametrize('body', [{'toptags': {'tag': 'bad'}}, {'toptags': {'tag': [1]}},
                               {'toptags': {'tag': [{'name': 123}]}}])
def test_malformed_nested_results_are_not_cached(boundary, body):
    b = boundary(lambda request: body)
    with net.probe_errors() as errors:
        assert lastfm.artist_genre('Artist', 'secret') is None
    assert errors and not b.iao._cache['api'] and not lastfm.tripped()


def test_missing_page_is_cached_without_a_retry_or_api_outage(boundary):
    def fetch(request):
        raise http(404)
    b = boundary(fetch)
    with net.probe_errors() as errors:
        assert lastfm._fetch_text('https://www.last.fm/music/Artist/Album') == ''
        assert lastfm._fetch_text('https://www.last.fm/music/Artist/Album') == ''
    assert not errors and len(b.calls) == 1
    assert not lastfm.tripped()


@pytest.mark.parametrize('url', ['https://example.com/a', 'file:///tmp/a',
                               'https://last.fm@example.com/a', 'https://last.fm.evil.test/a'])
def test_album_url_cannot_broaden_the_vendor_boundary(boundary, url):
    b = boundary(lambda request: pytest.fail('Invalid URL reached transport'))
    with pytest.raises(ValueError):
        b.iao.page(url)
    assert not b.calls


@pytest.mark.parametrize('failure', [WorkClosing('closing'), WorkBudgetExceeded('budget')])
def test_cancellation_does_not_become_negative_metadata_or_outage(boundary, failure):
    def fetch(request):
        raise failure
    b = boundary(fetch)
    with net.probe_errors() as errors, pytest.raises(type(failure)):
        lastfm.artist_genre('Artist', 'secret')
    assert not errors and not b.iao._cache['api'] and not lastfm.tripped()


def test_overlap_shares_one_refresh_and_does_not_hold_the_database_lock(boundary, store):
    entered, release, second_started = Event(), Event(), Event()

    def fetch(request):
        assert not store._lock.held_by_current_thread
        entered.set()
        assert release.wait(5)
        return tags()

    def lookup(second=False):
        if second:
            second_started.set()
        with lastfm.access(store):
            return lastfm.artist_genre('Artist', 'secret')

    b = boundary(fetch)
    store.settings.set_setting('lastfm_api_key', 'secret')
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(lookup)
        try:
            assert entered.wait(5)
            other = pool.submit(lookup, True)
            assert second_started.wait(5)
            pool.submit(store.settings.set_setting, 'during_lastfm', 'ok').result(2)
        finally:
            release.set()
        assert first.result(5) == other.result(5) == 'House'
    assert len(b.calls) == 1


def test_bounded_lru_cache_retains_recent_requests(boundary, monkeypatch):
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)
    b = boundary(lambda request: tags())
    for artist in ('A', 'B', 'A', 'C', 'A', 'B'):
        b.iao.get(query(artist=artist))
    assert len(b.calls) == 4 and len(b.iao._cache['api']) == 2


def test_slow_album_page_does_not_hold_up_healthy_api_requests(boundary, store):
    entered, release = Event(), Event()

    def fetch(request):
        if urllib.parse.urlsplit(request.full_url).hostname == 'www.last.fm':
            entered.set()
            assert release.wait(5)
            return b'<dt>Release Date</dt><dd>1996</dd>'
        return tags()
    b = boundary(fetch)
    store.settings.set_setting('lastfm_api_key', 'secret')

    def artist():
        with lastfm.access(store):
            return lastfm.artist_genre('Artist', 'secret')
    with ThreadPoolExecutor(max_workers=2) as pool:
        page = pool.submit(lastfm._fetch_text, 'https://www.last.fm/music/Artist/Album')
        try:
            assert entered.wait(5)
            assert pool.submit(artist).result(2) == 'House'
        finally:
            release.set()
        assert '1996' in page.result(5)
    assert len(b.calls) == len(b.paces) == 2


def test_waterfall_leaves_lastfm_pending_and_finishes_healthy_provider(boundary, store):
    def fetch(request):
        raise http(503, {'Retry-After': '120'})
    b = boundary(fetch)
    store.settings.set_setting('lastfm_api_key', 'secret')
    identity = store.identities.upsert_identity('main', 'cred', None, True)
    playlist = store.playlists.upsert_playlist(identity, 'PL', 'Mix', 4, 'h', 1000)
    tracks = [store.tracks.upsert_track(f'v{i}', f'Song{i}', 'Artist', None, None) for i in range(4)]
    store.playlists.set_playlist_tracks(playlist, tracks)
    healthy_calls = []
    healthy = SimpleNamespace(name='deezer', available=lambda store: True, reset=lambda: None,
        tripped=lambda: False, probe=lambda track, store: healthy_calls.append(track['id'])
        or EnrichmentResult('deezer', {'year': '1996'}))
    events = []
    completed = run_waterfall(store, store.tracks.tracks_for_waterfall(playlist),
        [{'name': 'lastfm', 'enabled': True}, {'name': 'deezer', 'enabled': True}], events.append,
        registry={'lastfm': lastfm, 'deezer': healthy}, now_fn=lambda: b.clock[0])
    assert not completed and len(b.calls) == 1 and len(healthy_calls) == 4
    attempt = store.enrichment_state.enrichment_attempts('song0|artist')['lastfm']
    assert attempt['status'] == 'retry'
    assert store.enrichment_state.integration_cooldown('lastfm')['retry_at'] == 1120
    assert all(row['year'] == '1996' for row in store.charts.playlist_tracks_detail(playlist))
    assert any(event.get('warning') for event in events)


def test_album_failure_retains_genre_and_a_later_run_completes_year(boundary, store):
    def fetch(request):
        if urllib.parse.urlsplit(request.full_url).hostname == 'ws.audioscrobbler.com':
            return {'track': {'name': 'Song', 'artist': {'name': 'Artist'},
                    'album': {'url': 'https://www.last.fm/music/Artist/Album'},
                    'toptags': {'tag': [{'name': 'house'}]}}}
        if len(b.calls) == 2:
            raise http(503, {'Retry-After': '120'})
        return b'<dt>Release Date</dt><dd>11 March 1996</dd>'
    b = boundary(fetch)
    store.settings.set_setting('lastfm_api_key', 'secret')
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    config = [{'name': 'lastfm', 'enabled': True}]

    def run():
        return run_waterfall(store, [store.tracks.track_for_waterfall(tid)], config, lambda event: None,
                             registry={'lastfm': lastfm}, now_fn=lambda: b.clock[0])
    assert not run()
    assert store.enrichment_state.enrichment_attempts('song|artist')['lastfm']['status'] == 'retry'
    assert store.tracks.get_track_enrichment(tid) == ('House', '')
    assert store.enrichment_state.integration_cooldown('lastfm') is None
    b.clock[0] += 120
    assert run()
    attempt = store.enrichment_state.enrichment_attempts('song|artist')['lastfm']
    assert attempt['status'] == 'found' and attempt['fields']['year'] == '1996'
    assert len(b.calls) == 3  # The retained track API result is reused.
    assert store.enrichment_state.integration_cooldown('lastfm:pages') is None
