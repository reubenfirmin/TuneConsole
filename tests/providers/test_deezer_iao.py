"""Deezer admission and recovery through real callers with controlled HTTP."""
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
from yt_playlist.core.store import Store
from yt_playlist.integrations import deezer as access
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.providers import deezer
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.rec import road_trip_candidates as candidates
from yt_playlist.util import net


def track(**changes):
    return {'id': 42, 'title': 'Song', 'artist': {'name': 'Artist'}, 'bpm': 120,
            'rank': 10, 'gain': -7, 'duration': 200, 'release_date': '1990-01-01',
            'album': {'id': 99, 'cover_xl': 'https://example.test/cover.jpg'}, **changes}


def catalog(request):
    path = urllib.parse.urlsplit(request.full_url).path
    if path == '/search/track':
        return {'data': [{'id': 42, 'title': 'Song', 'artist': {'name': 'Artist'}}]}
    if path == '/track/42':
        return track()
    if path == '/album/99':
        return {'id': 99, 'label': 'Label', 'genres': {'data': [{'name': 'Rock'}]}}
    raise AssertionError(path)


def http(code, headers=None):
    return urllib.error.HTTPError(access.API + '/search/track', code, 'controlled error', headers or {}, None)


@pytest.fixture
def boundary(monkeypatch, store):
    candidates._FACTS_CACHE.clear()

    def install(fetch=catalog):
        clock, calls, responses, events = [1000.0], [], [], []

        def pace():
            assert not store._lock.held_by_current_thread
            assert not store.conn.in_transaction
            events.append('pace')

        def open_request(request, *, timeout):
            assert not store._lock.held_by_current_thread
            assert not store.conn.in_transaction
            events.append('request')
            calls.append((request, timeout))
            body = fetch(request)
            headers = {}
            if isinstance(body, tuple):
                body, headers = body
            response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
            response.headers = headers
            responses.append(response)
            return response

        iao = access.DeezerIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=pace))
        monkeypatch.setattr(deezer, '_iao', iao)
        monkeypatch.setattr(candidates, 'monotonic', lambda: clock[0])
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, events=events)

    yield install
    candidates._FACTS_CACHE.clear()


def subject(store, title='Song'):
    entry = store.tracks.upsert_track(title, title, 'Artist', None, 200)
    return store.tracks.track_for_waterfall(entry)


def test_enrichment_and_road_trip_share_raw_responses_and_close_transport(boundary, store):
    b = boundary()
    result = deezer.probe({'title': 'Song', 'artist': 'Artist', 'duration_s': 200}, store)
    assert result.fields['bpm'] == 120 and result.fields['genre'] == 'Rock'
    facts = candidates.track_facts(store, 'Song', 'Artist')
    assert facts['duration'] == 200 and facts['year'] == 1990
    assert len(b.calls) == 3 and b.events == ['pace', 'request'] * 3
    for request, timeout in b.calls:
        assert timeout == access.TIMEOUT_S == 20
        assert request.get_header('User-agent') == access.USER_AGENT
        assert request.get_header('Accept') == 'application/json'
    assert all(response.closed for response in b.responses)
    assert access.DeezerIAO().pacer.min_interval == .15


def test_candidate_acquisition_and_ranking_persist_access_waits(boundary, store, monkeypatch):
    def failed(request):
        raise TimeoutError('controlled outage')

    b = boundary(failed)
    monkeypatch.setattr(candidates, 'artist_genre', lambda store, artist: None)
    rows = [{'video_id': 'video', 'title': 'Song', 'artist': 'Artist', 'album': '',
             'thumbnail': None, 'duration': 200, 'genre': ''}]
    with net.probe_errors() as errors:
        result = candidates.to_candidates(rows, store)
        assert candidates.rank_other(result, store)[0]['video_id'] == 'video'
    assert errors and len(b.calls) == 1
    assert store.enrichment_state.integration_cooldown('deezer')['retry_at'] == 1030
    # A separate access object must still enforce the wait before enrichment.
    replacement = access.DeezerIAO(now_fn=lambda: 1000,
        open_fn=lambda *a, **kw: pytest.fail('Enrichment ignored candidate-access outage'),
        pacer=SimpleNamespace(wait=lambda: None))
    monkeypatch.setattr(deezer, '_iao', replacement)
    with net.probe_errors() as deferred:
        assert not deezer.probe({'title': 'Other', 'artist': 'Artist'}, store).fields
    assert deferred and deferred[0].retry_at == 1030


@pytest.mark.parametrize('header,deadline', [
    ('120', 1120), ('7200', 8200),
    (format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True), 1120),
    (None, 1030), ('nonsense', 1030), ('inf', 1030), ('NaN', 1030),
])
def test_host_retry_after_is_shared_with_candidates_and_new_instances(boundary, store, header, deadline):
    def failed(request):
        raise http(503, {'Retry-After': header} if header is not None else {})

    b = boundary(failed)
    with net.probe_errors() as first:
        assert not deezer.probe({'title': 'Song', 'artist': 'Artist'}, store).fields
    with net.probe_errors() as second:
        assert not any(candidates.track_facts(store, 'Other', 'Artist').values())
    assert len(b.calls) == 1
    assert len(first) == len(second) == 1
    assert first[0].provider == second[0].provider == 'deezer'
    assert first[0].retry_at == second[0].retry_at == deadline
    assert store.enrichment_state.integration_cooldown('deezer')['failures'] == 1
    deezer.reset()
    assert deezer.available(store) and deezer.tripped()
    restarted = access.DeezerIAO(now_fn=lambda: b.clock[0],
        open_fn=lambda *a, **kw: pytest.fail('New instance ignored persisted deadline'),
        pacer=SimpleNamespace(wait=lambda: pytest.fail('Blocked work was paced')))
    with restarted.context(store.enrichment_state), pytest.raises(IntegrationUnavailable) as caught:
        restarted.get(access.API + '/search/track?q=new')
    assert caught.value.retry_at == deadline


@pytest.mark.parametrize('code', [4, 700])
def test_http_200_host_errors_defer_every_endpoint(boundary, store, code):
    b = boundary(lambda request: ({'error': {'code': code, 'message': 'query must not persist'}},
                                  {'Retry-After': '90'}))
    with deezer.access(store), net.probe_errors() as errors:
        for url in ('/search/track?q=Song', '/track/42', '/album/99'):
            with pytest.raises(IntegrationUnavailable):
                b.iao.get(access.API + url)
    assert len(b.calls) == 1 and all(net.provider_outage(error) for error in errors)
    assert all(error.retry_at == 1090 for error in errors)
    saved = store.enrichment_state.integration_cooldown('deezer')
    assert saved['failures'] == 1 and 'query must not persist' not in saved['error']
    assert b.responses[0].closed


@pytest.mark.parametrize('body', [b'bad json', {}, {'data': None}, {'data': ['bad']},
    {'error': 'bad'}, {'error': {'code': 500}}, {'error': {'message': 'unknown'}}])
def test_invalid_searches_retry_without_poisoning_other_queries(boundary, store, body):
    def fetch(request):
        return body if 'bad' in request.full_url else {'data': []}

    b = boundary(fetch)
    with deezer.access(store), net.probe_errors() as errors:
        for _ in range(2):
            with pytest.raises(IntegrationUnavailable) as caught:
                b.iao.get(access.API + '/search/track?q=bad')
            assert not caught.value.outage and caught.value.retry_at == 1030
        assert b.iao.get(access.API + '/search/track?q=healthy') == {'data': []}
    assert len(errors) == 2 and len(b.calls) == 2
    assert store.enrichment_state.integration_cooldown('deezer') is None
    b.clock[0] = 1030
    with pytest.raises(IntegrationUnavailable):
        b.iao.get(access.API + '/search/track?q=bad')
    assert len(b.calls) == 3
    assert all(response.closed for response in b.responses)


@pytest.mark.parametrize('path,body', [
    ('/search/track?q=Song', {'data': []}),
    ('/search/track?q=Song', {'error': {'code': 800}}),
    ('/track/42', {'error': {'code': '800'}}),
    ('/album/99', {'error': {'code': 800}}),
    ('/track/42', http(404)),
])
def test_confirmed_absence_is_cached_without_an_outage(boundary, path, body):
    def fetch(request):
        if isinstance(body, Exception):
            raise body
        return body

    b = boundary(fetch)
    with net.probe_errors() as errors:
        first = b.iao.get(access.API + path)
        b.clock[0] += access.MISS_TTL - 1
        assert b.iao.get(access.API + path) == first
        b.clock[0] += 1
        assert b.iao.get(access.API + path) == first
    assert not errors and len(b.calls) == 2


@pytest.mark.parametrize('code', [404, 429, 503])
def test_http_error_response_bodies_are_closed(boundary, code):
    body = io.BytesIO(b'controlled error body')

    def fetch(request):
        raise urllib.error.HTTPError(request.full_url, code, 'controlled error', {}, body)

    b = boundary(fetch)
    if code == 404:
        assert b.iao.get(access.API + '/track/42') == {'error': {'code': 800}}
    else:
        with pytest.raises(IntegrationUnavailable):
            b.iao.get(access.API + '/track/42')
    assert body.closed


@pytest.mark.parametrize('path,body', [
    ('/track/42', track(id=43)),
    ('/track/42', track(artist=None)),
    ('/track/42', track(contributors={})),
    ('/track/42', track(bpm='unknown')),
    ('/track/42', track(album={'id': 'bad'})),
    ('/album/99', {'id': 100}),
    ('/album/99', {'id': 99, 'genres': {'data': ['bad']}}),
])
def test_invalid_detail_responses_remain_retryable(boundary, store, path, body):
    b = boundary(lambda request: body)
    with deezer.access(store), net.probe_errors() as errors:
        for _ in range(2):
            with pytest.raises(IntegrationUnavailable) as caught:
                b.iao.get(access.API + path)
            assert not caught.value.outage
    assert len(b.calls) == 1 and len(errors) == 2 and not b.iao._cache
    assert store.enrichment_state.integration_cooldown('deezer') is None


@pytest.mark.parametrize('path', ['/search/track?q=Song', '/track/42', '/album/99'])
def test_malformed_refresh_retains_last_good_evidence_with_retry(boundary, path):
    malformed = [False]
    b = boundary(lambda request: {} if malformed[0] else catalog(request))
    url = access.API + path
    expected = b.iao.get(url)
    b.clock[0] += access.POSITIVE_TTL
    malformed[0] = True
    with net.probe_errors() as errors:
        assert b.iao.get(url) == expected
        assert b.iao.get(url) == expected
    assert len(b.calls) == 2 and len(errors) == 2
    assert all(not error.outage and error.retry_at == b.clock[0] + 30 for error in errors)
    malformed[0] = False
    b.clock[0] += 30
    with net.probe_errors() as recovered:
        assert b.iao.get(url) == expected
    assert not recovered and len(b.calls) == 3


@pytest.mark.parametrize('body', [{}, {'error': 'temporary backend failure'}])
def test_invalid_search_remains_pending_in_waterfall(boundary, store, body):
    b = boundary(lambda request: body)
    item = subject(store)
    completed = run_waterfall(store, [item], [{'name': 'deezer', 'enabled': True}],
        lambda event: None, registry={'deezer': deezer}, now_fn=lambda: b.clock[0])
    state = store.enrichment_state.enrichment_provider_state(item['recording_id'])['deezer']
    assert state['status'] == 'retry' and state['retry_at'] == 1030
    assert not completed and not b.iao._cache and len(b.calls) == 1


def test_cache_identity_detachment_expiry_and_lru_are_owned_by_iao(boundary, monkeypatch):
    b = boundary(lambda request: {'data': [{'id': 42, 'title': 'Song'}]})
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)
    a = access.API + '/search/track?q=A&limit=10'
    first = b.iao.get(a)
    first['data'][0]['title'] = 'mutated'
    assert b.iao.get(access.API + '/search/track?limit=10&q=A')['data'][0]['title'] == 'Song'
    b.iao.get(access.API + '/search/track?q=B&limit=10')
    b.iao.get(a)
    b.iao.get(access.API + '/search/track?q=A&limit=5')
    b.iao.get(access.API + '/search/track?q=B&limit=10')
    assert len(b.calls) == 4 and len(b.iao._cache) == 2
    b.clock[0] += access.POSITIVE_TTL
    b.iao.get(access.API + '/search/track?q=B&limit=10')
    assert len(b.calls) == 5


def test_response_ttl_starts_after_the_request_finishes(boundary):
    def fetch(request):
        b.clock[0] += 20
        return {'data': []}

    b = boundary(fetch)
    url = access.API + '/search/track?q=Song'
    assert b.iao.get(url) == {'data': []}
    b.clock[0] = 1020 + access.MISS_TTL - 1
    b.iao.get(url)
    assert len(b.calls) == 1
    b.clock[0] += 1
    b.iao.get(url)
    assert len(b.calls) == 2


def test_expired_good_evidence_survives_failed_refresh_with_retry_signal(boundary, store):
    failing = [False]

    def fetch(request):
        if failing[0]:
            raise TimeoutError('controlled outage')
        return catalog(request)

    b = boundary(fetch)
    expected = candidates.track_facts(store, 'Song', 'Artist')
    failing[0] = True
    b.clock[0] += access.POSITIVE_TTL
    with net.probe_errors() as errors:
        assert candidates.track_facts(store, 'Song', 'Artist') == expected
    assert errors and len(b.calls) == 4
    assert store.enrichment_state.integration_cooldown('deezer')['failures'] == 1
    # Derived domain-cache hits must retain the retry outcome as well as facts.
    with net.probe_errors() as retained:
        assert candidates.track_facts(store, 'Song', 'Artist') == expected
    assert retained and all(error.provider == 'deezer' for error in retained)
    assert len(b.calls) == 4
    failing[0] = False
    b.clock[0] += candidates._METADATA_RETRY_TTL
    with net.probe_errors() as recovered:
        assert candidates.track_facts(store, 'Song', 'Artist') == expected
    assert not recovered and store.enrichment_state.integration_cooldown('deezer') is None


def test_album_outage_keeps_track_fields_pending_and_other_provider_progress(boundary, store):
    def fetch(request):
        if '/album/' in request.full_url:
            raise http(503, {'Retry-After': '120'})
        return catalog(request)

    b = boundary(fetch)
    tracks = [subject(store), subject(store, 'Other')]
    healthy_calls = []
    healthy = SimpleNamespace(name='acousticbrainz', available=lambda store: True,
        reset=lambda: None, tripped=lambda: False,
        probe=lambda item, store: healthy_calls.append(item['title'])
        or EnrichmentResult('acousticbrainz', {'energy': .5}))
    config = [{'name': provider.name, 'enabled': True} for provider in (deezer, healthy)]
    run_waterfall(store, tracks, config, lambda event: None,
                  registry={'deezer': deezer, healthy.name: healthy}, now_fn=lambda: b.clock[0])
    state = store.enrichment_state.enrichment_provider_state(tracks[0]['recording_id'])
    assert state['deezer']['status'] == 'retry' and state['deezer']['fields']['bpm'] == 120
    assert state['deezer']['fields']['art'] == 'https://example.test/cover.jpg'
    assert state['deezer']['retry_at'] == 1120
    assert healthy_calls == ['Song', 'Other']
    assert len(b.calls) == 3
    assert store.enrichment_state.integration_cooldown('deezer')['failures'] == 1
    assert store.enrichment_state.integration_cooldown('acousticbrainz') is None


def test_concurrent_distinct_callers_share_the_first_outage(boundary, store):
    def failed(request):
        raise TimeoutError('controlled outage')

    b = boundary(failed)

    def read(index):
        with net.probe_errors() as errors:
            if index % 2:
                candidates.track_facts(store, f'Song {index}', 'Artist')
            else:
                deezer.probe({'title': f'Song {index}', 'artist': 'Artist'}, store)
        return errors

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(8)))
    assert len(b.calls) == 1
    assert all(len(errors) == 1 and errors[0].retry_at == 1030 for errors in results)
    assert store.enrichment_state.integration_cooldown('deezer')['failures'] == 1


def test_foreground_write_proceeds_during_vendor_io(boundary, store):
    entered, release = Event(), Event()

    def fetch(request):
        entered.set()
        assert release.wait(5), 'Serving connection was held during vendor access'
        return {'data': []}

    b = boundary(fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(candidates.track_facts, store, 'Song', 'Artist')
        try:
            assert entered.wait(5)
            assert pool.submit(store.settings.set_setting, 'during-deezer', 'saved').result(2) is None
            assert store.settings.get_setting('during-deezer') == 'saved'
        finally:
            release.set()
        assert not any(work.result(5).values())
    assert len(b.calls) == 1


def test_cooldown_survives_database_reopen_and_recovery_clears_only_observed_wait(boundary, tmp_path):
    b = boundary(lambda request: {'data': []})
    path = tmp_path / 'deezer.db'
    db = Store(path)
    db.init_schema()
    try:
        db.enrichment_state.defer_integration('deezer', now=1000, retry_after=1100, error='old outage')
    finally:
        db.conn.close()
    db = Store(path)
    db.init_schema()
    try:
        with deezer.access(db):
            with pytest.raises(IntegrationUnavailable):
                b.iao.get(access.API + '/search/track?q=Song')
            assert not b.calls
            b.clock[0] = 1100
            b.iao.get(access.API + '/search/track?q=Song')
            assert db.enrichment_state.integration_cooldown('deezer') is None
            db.enrichment_state.defer_integration('deezer', now=1101, retry_after=1200, error='new outage')
            db.enrichment_state.clear_integration_cooldown('deezer', 1100)
            assert db.enrichment_state.integration_cooldown('deezer')['retry_at'] == 1200
            b.iao.get(access.API + '/search/track?q=Song')  # A cache hit must not clear it.
            assert db.enrichment_state.integration_cooldown('deezer')['retry_at'] == 1200
    finally:
        db.conn.close()


def test_recovery_response_cannot_clear_an_outage_persisted_during_io(boundary, store):
    def fetch(request):
        store.enrichment_state.defer_integration('deezer', now=1101, retry_after=1200, error='new outage')
        return {'data': []}

    b = boundary(fetch)
    store.enrichment_state.defer_integration('deezer', now=1000, retry_after=1100, error='old outage')
    b.clock[0] = 1100
    with deezer.access(store):
        assert b.iao.get(access.API + '/search/track?q=Song') == {'data': []}
        assert store.enrichment_state.integration_cooldown('deezer')['retry_at'] == 1200
        assert b.iao.get(access.API + '/search/track?q=Song') == {'data': []}
        with pytest.raises(IntegrationUnavailable) as caught:
            b.iao.get(access.API + '/search/track?q=Other')
    assert caught.value.retry_at == 1200 and len(b.calls) == 1


def test_scoped_repository_does_not_remain_bound_to_standalone_calls(boundary, store):
    b = boundary(lambda request: {'data': []})
    store.enrichment_state.defer_integration('deezer', now=1000, retry_after=1100, error='saved outage')
    with deezer.access(store):
        with deezer.access(), pytest.raises(IntegrationUnavailable):
            b.iao.get(access.API + '/search/track?q=Song')
    assert b.iao.get(access.API + '/search/track?q=Song') == {'data': []}
    assert len(b.calls) == 1
    assert store.enrichment_state.integration_cooldown('deezer')['retry_at'] == 1100


@pytest.mark.parametrize('error_type', [WorkClosing, WorkBudgetExceeded])
@pytest.mark.parametrize('stage', ['search', 'track', 'album'])
def test_cancellation_never_becomes_a_completed_miss(boundary, store, error_type, stage):
    def fetch(request):
        path = urllib.parse.urlsplit(request.full_url).path
        if path.split('/')[1] == stage:
            raise error_type('controlled cancellation')
        return catalog(request)

    b = boundary(fetch)
    item = subject(store)
    with net.probe_errors() as errors, pytest.raises(error_type):
        run_waterfall(store, [item], [{'name': 'deezer', 'enabled': True}], lambda event: None,
                      registry={'deezer': deezer}, now_fn=lambda: b.clock[0])
    assert not errors and store.enrichment_state.integration_cooldown('deezer') is None
    assert not store.enrichment_state.enrichment_attempts(item['identity_key'])
    assert all(response.closed for response in b.responses)


@pytest.mark.parametrize('url', ['http://api.deezer.com/track/42', 'https://other.test/track/42',
    access.API + '/track/42?q=a&q=b', access.API + '/track/42#fragment', access.API + '/user/me'])
def test_only_supported_public_catalog_endpoints_can_reach_transport(boundary, url):
    b = boundary()
    with pytest.raises(ValueError, match='Unsupported Deezer'):
        b.iao.get(url)
    assert not b.calls and not b.events
