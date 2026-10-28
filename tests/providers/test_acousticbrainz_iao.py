"""AcousticBrainz boundary checks with isolated stores and controlled transport."""
import io
import json
import urllib.error
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from threading import Event
from types import SimpleNamespace

import pytest

from tests.test_acousticbrainz import HIGHLEVEL, LOWLEVEL
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations import acousticbrainz as access
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.integrations.musicbrainz import MusicBrainzIAO
from yt_playlist.providers import acousticbrainz as ab, musicbrainz
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.util import net

MBID = '12345678-1234-1234-1234-123456789abc'
OTHER = '22345678-1234-1234-1234-123456789abc'


def http(code, headers=None, body=None):
    return urllib.error.HTTPError('https://acousticbrainz.org/api/v1/', code,
                                  'controlled error', headers or {}, body)


def descriptors(request):
    return HIGHLEVEL if request.full_url.endswith('high-level') else LOWLEVEL


@pytest.fixture
def boundary(monkeypatch):
    def install(fetch=descriptors):
        clock, calls, responses, paces = [1000.0], [], [], []

        def open_request(request, *, timeout):
            calls.append((request, timeout))
            body = fetch(request)
            response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
            responses.append(response)
            return response

        iao = access.AcousticBrainzIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=lambda: paces.append(clock[0])))
        monkeypatch.setattr(ab, '_iao', iao)
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, paces=paces)
    return install


def test_both_entry_points_share_detached_cache_and_complete_metadata(boundary, store):
    b = boundary()
    result = ab.probe({'mb_recording_id': MBID}, store)
    assert result.fields['bpm'] == 128 and result.fields['energy'] == .709
    assert len(b.calls) == 2
    assert ab.enrich(MBID.upper())['bpm'] == 128
    with ab.access(store):
        low = b.iao.low_level(MBID)
    low.clear()
    assert b.iao.low_level(MBID) == LOWLEVEL and len(b.calls) == 2
    assert all(response.closed for response in b.responses)
    assert len(b.paces) == 2 and access.MIN_INTERVAL == .5
    for request, timeout in b.calls:
        assert timeout == access.TIMEOUT_S == 20
        assert request.get_header('User-agent') == access.USER_AGENT
        assert request.get_header('Accept') == 'application/json'


@pytest.mark.parametrize('failure', [TimeoutError(), http(503), http(429)])
def test_outage_is_shared_between_endpoints_and_other_recordings(boundary, store, failure):
    def fetch(request):
        if len(b.calls) == 1:
            raise failure
        return descriptors(request)
    b = boundary(fetch)
    with net.probe_errors() as errors:
        assert ab.probe({'mb_recording_id': MBID}, store).fields == {'mb_recording_id': MBID}
        assert ab.enrich(OTHER) == ab._empty()
    assert len(b.calls) == 1 and len(errors) == 4
    assert all(error.provider == 'acousticbrainz' and error.retry_at == 1030 for error in errors)
    assert store.enrichment_state.integration_cooldown('acousticbrainz')['retry_at'] == 1030
    ab.reset()
    assert ab.tripped()
    b.clock[0] = 1030
    assert ab.probe({'mb_recording_id': MBID}, store).fields['energy'] == .709
    assert len(b.calls) == 3 and not ab.tripped()
    assert store.enrichment_state.integration_cooldown('acousticbrainz') is None


@pytest.mark.parametrize('header,expected', [
    ('120', 1120),
    (format_datetime(datetime.fromtimestamp(1600, tz=timezone.utc), usegmt=True), 1600),
    ('invalid', 1030), ('inf', 1030), ('-5', 1030),
])
def test_retry_after_survives_recreation(boundary, store, header, expected):
    def fetch(request):
        raise http(429, {'Retry-After': header})
    b = boundary(fetch)
    with b.iao.context(store), pytest.raises(IntegrationUnavailable) as first:
        b.iao.low_level(MBID)
    assert first.value.retry_at == expected
    recreated = access.AcousticBrainzIAO(now_fn=lambda: b.clock[0],
        open_fn=lambda *args, **kwargs: pytest.fail('Persisted cooldown bypassed'),
        pacer=SimpleNamespace(wait=lambda: pytest.fail('Deferred request should not pace')))
    with recreated.context(store), pytest.raises(IntegrationUnavailable) as deferred:
        recreated.high_level(OTHER)
    assert deferred.value.retry_at == expected


def test_partial_outage_reuses_successful_half_until_missing_half_recovers(boundary, store):
    def fetch(request):
        if len(b.calls) == 2:
            raise http(503, {'Retry-After': '120'})
        return descriptors(request)
    b = boundary(fetch)
    with net.probe_errors() as errors:
        first = ab.probe({'mb_recording_id': MBID}, store)
        second = ab.probe({'mb_recording_id': MBID}, store)
    assert first.fields['bpm'] == second.fields['bpm'] == 128
    assert 'energy' not in first.fields and len(b.calls) == 2 and len(errors) == 2
    b.clock[0] = 1120
    assert ab.probe({'mb_recording_id': MBID}, store).fields['energy'] == .709
    assert len(b.calls) == 3 and b.calls[-1][0].full_url.endswith('/high-level')


@pytest.mark.parametrize('endpoint', ['low-level', 'high-level'])
def test_only_missing_endpoint_is_negative_cached(boundary, endpoint):
    def fetch(request):
        if request.full_url.endswith(endpoint):
            raise http(404)
        return descriptors(request)
    b = boundary(fetch)
    with net.probe_errors() as errors:
        first = ab.enrich(MBID)
        assert ab.enrich(MBID) == first
    assert not errors and len(b.calls) == 2 and not ab.tripped()
    assert first['bpm' if endpoint == 'high-level' else 'energy'] is not None
    b.clock[0] += access.MISS_TTL
    ab.enrich(MBID)
    assert len(b.calls) == 3 and b.calls[-1][0].full_url.endswith(endpoint)


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('status', [404, 400, 429, 503])
def test_http_error_body_is_closed_on_miss_retry_and_retained_data(boundary, cached, status):
    b = boundary()
    if cached:
        assert b.iao.low_level(MBID) == LOWLEVEL
        b.clock[0] += access.POSITIVE_TTL
    body = io.BytesIO(b'controlled vendor error')

    def fail(request):
        raise http(status, {'Retry-After': '120'}, body)

    b.iao.open = lambda request, **kwargs: fail(request)
    with net.probe_errors() as errors:
        if status == 404:
            assert b.iao.low_level(MBID) == {}
        elif cached:
            assert b.iao.low_level(MBID) == LOWLEVEL
        else:
            with pytest.raises(IntegrationUnavailable):
                b.iao.low_level(MBID)
    assert body.closed
    if status == 404:
        assert not errors
    else:
        assert len(errors) == 1 and errors[0].retry_at == b.clock[0] + 120
        assert errors[0].outage == (status in (429, 503))


@pytest.mark.parametrize('section,field', [
    ('rhythm', 'bpm'), ('lowlevel', 'average_loudness'), ('lowlevel', 'dynamic_complexity'),
])
@pytest.mark.parametrize('bad', ['temporary backend failure', '', None, True, [], {}, float('nan'), float('inf')])
def test_present_non_numeric_low_level_descriptor_is_pending(boundary, section, field, bad):
    payload = deepcopy(LOWLEVEL)
    payload[section][field] = bad
    b = boundary(lambda request: payload)
    with net.probe_errors() as errors, pytest.raises(IntegrationUnavailable):
        b.iao.low_level(MBID)
    assert len(errors) == 1 and not errors[0].outage
    assert not b.iao._cache


@pytest.mark.parametrize('field', ['key_key', 'key_scale'])
@pytest.mark.parametrize('bad', [None, [], 123, '', '   '])
def test_present_malformed_tonal_descriptor_is_pending(boundary, field, bad):
    payload = deepcopy(LOWLEVEL)
    payload['tonal'][field] = bad
    b = boundary(lambda request: payload)
    with net.probe_errors() as errors, pytest.raises(IntegrationUnavailable):
        b.iao.low_level(MBID)
    assert len(errors) == 1 and not b.iao._cache


@pytest.mark.parametrize('model,label', [
    ('danceability', 'danceable'), ('mood_party', 'party'), ('mood_aggressive', 'aggressive'),
    ('mood_happy', 'happy'), ('mood_sad', 'sad'), ('mood_relaxed', 'relaxed'),
    ('mood_acoustic', 'acoustic'), ('voice_instrumental', 'instrumental'),
])
def test_each_consumed_classifier_rejects_invalid_present_probability(boundary, model, label):
    payload = deepcopy(HIGHLEVEL)
    payload['highlevel'][model]['all'][label] = 'busy'
    b = boundary(lambda request: payload)
    with net.probe_errors() as errors, pytest.raises(IntegrationUnavailable):
        b.iao.high_level(MBID)
    assert len(errors) == 1 and not errors[0].outage and not b.iao._cache


@pytest.mark.parametrize('bad', [None, True, [], {}, -0.1, 1.1, float('inf'), float('nan')])
def test_classifier_probability_must_be_finite_and_in_range(boundary, bad):
    payload = {'highlevel': {'danceability': {'all': {'danceable': bad}}}}
    b = boundary(lambda request: payload)
    with net.probe_errors() as errors, pytest.raises(IntegrationUnavailable):
        b.iao.high_level(MBID)
    assert len(errors) == 1 and not b.iao._cache


@pytest.mark.parametrize('endpoint,payload', [
    ('low-level', {'rhythm': {}, 'tonal': {}, 'lowlevel': {}}),
    ('high-level', {'highlevel': {'danceability': {'all': {}}, 'mood_happy': {}}}),
    ('low-level', {'rhythm': {'bpm': '128'}, 'lowlevel': {'average_loudness': 0}}),
    ('high-level', {'highlevel': {'danceability': {'all': {'danceable': '0.92'}}}}),
])
def test_absent_optional_fields_and_supported_numeric_strings_remain_valid(boundary, endpoint, payload):
    b = boundary(lambda request: payload)
    with net.probe_errors() as errors:
        assert b.iao.get(MBID, endpoint) == payload
        assert b.iao.get(MBID, endpoint) == payload
    assert not errors and len(b.calls) == 1


@pytest.mark.parametrize('endpoint,bad', [
    ('low-level', {'rhythm': {'bpm': 'temporary backend failure'}}),
    ('high-level', {'highlevel': {'danceability': {'all': {'danceable': 'busy'}}}}),
])
def test_malformed_refresh_retains_last_good_and_recovers_without_blocking_other_work(boundary, endpoint, bad):
    b = boundary()
    before = ab.enrich(MBID)
    b.clock[0] += access.POSITIVE_TTL
    b.iao.open = lambda request, **kwargs: io.StringIO(json.dumps(
        bad if MBID in request.full_url and request.full_url.endswith(endpoint) else descriptors(request)))
    with net.probe_errors() as errors:
        assert ab.enrich(MBID) == before
        assert ab.enrich(OTHER) == before
    assert len(errors) == 1 and not errors[0].outage and not ab.tripped()
    # The malformed endpoint is not stamped as a fresh day-long cache entry.
    assert b.iao._cache[(MBID, endpoint)][1] <= b.clock[0]
    b.clock[0] += 30
    b.iao.open = lambda request, **kwargs: io.StringIO(json.dumps(descriptors(request)))
    with net.probe_errors() as errors:
        assert ab.enrich(MBID) == before
    assert not errors and b.iao._cache[(MBID, endpoint)][1] == b.clock[0] + access.POSITIVE_TTL


@pytest.mark.parametrize('bad', [b'invalid json', [], {}, {'error': 'busy'},
    {'highlevel': None}, {'highlevel': {'danceability': None}},
    {'highlevel': {'danceability': {'all': []}}}])
def test_malformed_half_is_pending_but_unrelated_half_is_usable(boundary, bad):
    b = boundary(lambda request: bad if request.full_url.endswith('high-level') else LOWLEVEL)
    with net.probe_errors() as errors:
        assert ab.enrich(MBID)['bpm'] == 128
        assert ab.enrich(MBID)['energy'] is None
    assert len(errors) == 2 and all(not net.provider_outage(error) for error in errors)
    assert len(b.calls) == 2 and len(b.iao._cache) == 1
    b.clock[0] += 30
    ab.enrich(MBID)
    assert len(b.calls) == 3


def test_malformed_low_level_does_not_block_high_level_or_another_recording(boundary):
    def fetch(request):
        if MBID in request.full_url and request.full_url.endswith('low-level'):
            return {'rhythm': []}
        return descriptors(request)
    b = boundary(fetch)
    with net.probe_errors() as errors:
        feat = ab.enrich(MBID)
        assert ab.enrich(OTHER)['bpm'] == 128
    assert feat['bpm'] is None and feat['energy'] == .709
    assert len(errors) == 1 and not net.provider_outage(errors[0])
    assert len(b.calls) == 4


def test_success_does_not_clear_a_newer_persisted_host_deadline(boundary, store):
    def fetch(request):
        store.enrichment_state.defer_integration('acousticbrainz', now=1000,
            retry_after=2000, error='Another process saw throttling')
        return LOWLEVEL
    b = boundary(fetch)
    with b.iao.context(store):
        assert b.iao.low_level(MBID) == LOWLEVEL
        with pytest.raises(IntegrationUnavailable) as deferred:
            b.iao.high_level(MBID)
    assert deferred.value.retry_at == 2000 and len(b.calls) == 1
    assert store.enrichment_state.integration_cooldown('acousticbrainz')['retry_at'] == 2000


def test_stale_successful_halves_remain_useful_and_pending_through_failure(boundary):
    b = boundary()
    assert ab.enrich(MBID)['energy'] == .709
    b.clock[0] += access.POSITIVE_TTL

    def fail(request):
        raise http(503)
    b.iao.open = fail
    with net.probe_errors() as errors:
        feat = ab.enrich(MBID)
    assert feat['bpm'] == 128 and feat['energy'] == .709
    assert len(errors) == 2
    b.clock[0] += 30
    b.iao.open = lambda request, **kwargs: io.StringIO(json.dumps(descriptors(request)))
    with net.probe_errors() as errors:
        assert ab.enrich(MBID)['energy'] == .709
    assert not errors


def test_cache_ttl_starts_after_response_and_is_bounded(boundary, monkeypatch):
    def fetch(request):
        b.clock[0] += 20
        return descriptors(request)
    b = boundary(fetch)
    b.iao.low_level(MBID)
    b.clock[0] = 1020 + access.POSITIVE_TTL - 1
    b.iao.low_level(MBID)
    assert len(b.calls) == 1
    b.clock[0] += 1
    b.iao.low_level(MBID)
    assert len(b.calls) == 2
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)
    for mbid, endpoint in ((MBID, 'high-level'), (MBID, 'low-level'),
                           (OTHER, 'low-level'), (MBID, 'low-level'), (MBID, 'high-level')):
        b.iao.get(mbid, endpoint)
    assert len(b.calls) == 5 and len(b.iao._cache) == 2


@pytest.mark.parametrize('failure', [WorkClosing(), WorkBudgetExceeded()])
def test_cancellation_propagates_without_retry_or_miss(boundary, failure):
    def fetch(request):
        raise failure
    b = boundary(fetch)
    with net.probe_errors() as errors, pytest.raises(type(failure)):
        ab.enrich(MBID)
    assert not errors and not b.iao._cache and not b.iao._waits


@pytest.mark.parametrize('url', ['http://acousticbrainz.org/api/v1/' + MBID + '/low-level',
    'https://example.test/api/v1/' + MBID + '/low-level',
    access.API + '/../low-level', access.API + '/' + MBID + '/other',
    access.API + '/' + MBID + '/low-level?extra=1'])
def test_boundary_rejects_resources_outside_existing_endpoints(boundary, url):
    b = boundary()
    with pytest.raises(ValueError):
        b.iao.get_url(url)
    assert not b.calls


def test_musicbrainz_outage_does_not_impose_acousticbrainz_cooldown(boundary, store, monkeypatch):
    b = boundary()
    mb_calls = []

    def fail(request, **kwargs):
        mb_calls.append(request.full_url)
        raise http(503, {'Retry-After': '120'})
    monkeypatch.setattr(musicbrainz, '_iao', MusicBrainzIAO(open_fn=fail,
        now_fn=lambda: b.clock[0], pacer=SimpleNamespace(wait=lambda: None)))
    with net.probe_errors() as errors:
        with pytest.raises(IntegrationUnavailable) as dependency:
            ab.probe({'title': 'Song', 'artist': 'Artist'}, store)
        assert dependency.value.provider == 'musicbrainz' and dependency.value.retry_at == 1120
        assert not b.calls and b.iao.availability().ready(b.clock[0])
        assert ab.probe({'mb_recording_id': MBID}, store).fields['bpm'] == 128
    assert len(mb_calls) == 1 and len(b.calls) == 2
    assert errors and all(error.provider == 'musicbrainz' for error in errors)
    assert set(store.enrichment_state.enrichment_cooldowns()) == {'musicbrainz'}


def test_overlapping_callers_coalesce_without_holding_the_database_lock(boundary, store):
    entered, release = Event(), Event()

    def fetch(request):
        assert not store._lock.held_by_current_thread
        entered.set()
        assert release.wait(5)
        return LOWLEVEL
    b = boundary(fetch)

    def read():
        with b.iao.context(store):
            return b.iao.low_level(MBID)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(read)
        try:
            assert entered.wait(5)
            second = pool.submit(read)
            pool.submit(store.settings.set_setting, 'unrelated', 'updated').result(2)
        finally:
            release.set()
        assert first.result(5) == second.result(5) == LOWLEVEL
    assert len(b.calls) == 1


def test_waterfall_retains_partial_findings_and_finishes_healthy_work(boundary, store):
    def fetch(request):
        if request.full_url.endswith('/high-level') and b.clock[0] == 1000:
            raise http(503, {'Retry-After': '120'})
        return descriptors(request)
    b = boundary(fetch)
    tracks = []
    for index in range(2):
        tid = store.tracks.upsert_track(f'v{index}', f'Song{index}', 'Artist', None, None)
        store.enrichment.apply_provider_fields(tid, 'musicbrainz', {'mb_recording_id': MBID})
        tracks.append(store.tracks.track_for_waterfall(tid))
    healthy_calls = []
    healthy = SimpleNamespace(name='deezer', available=lambda store: True, reset=lambda: None,
        tripped=lambda: False, probe=lambda track, store: healthy_calls.append(track['id'])
        or EnrichmentResult('deezer', {'genre': 'Rock'}))
    config = [{'name': 'acousticbrainz', 'enabled': True}, {'name': 'deezer', 'enabled': True}]
    registry = {'acousticbrainz': ab, 'deezer': healthy}
    assert not run_waterfall(store, tracks, config, lambda event: None,
                             registry=registry, now_fn=lambda: b.clock[0])
    assert len(healthy_calls) == 2 and len(b.calls) == 2
    assert store.tracks.get_track_audio(tracks[0]['id']) == (128, None, None)
    attempt = store.enrichment_state.enrichment_provider_state(tracks[0]['recording_id'])['acousticbrainz']
    assert attempt['status'] == 'retry' and attempt['retry_at'] == 1120
    b.clock[0] = 1120
    assert run_waterfall(store, tracks, config, lambda event: None,
                         registry=registry, now_fn=lambda: b.clock[0])
    assert store.tracks.get_track_audio(tracks[0]['id']) == (128, .709, .92)
    assert len(b.calls) == 3
