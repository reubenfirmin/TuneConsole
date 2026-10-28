"""Discogs access and caller recovery with controlled transport; no vendor calls."""
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
from yt_playlist.integrations import discogs as access
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.providers import discogs, enrichment, musicbrainz, release_notes
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.util import net


def release(identity=123):
    return {'id': identity, 'title': 'Album', 'artists': [{'name': 'Artist'}],
            'labels': [{'name': 'Label'}], 'tracklist': [{'title': 'Song'}],
            'community': {'rating': {'average': 4.5, 'count': 12}}}


def hits(*, found=True):
    return {'results': [{'id': 123, 'title': 'Artist - Song', 'year': '1996',
                         'style': ['House']}] if found else []}


def http(code, headers=None):
    return urllib.error.HTTPError('https://api.discogs.com/database/search', code,
                                  'controlled error', headers or {}, None)


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    monkeypatch.delenv('DISCOGS_TOKEN', raising=False)
    monkeypatch.setattr(access.credentials.paths, 'config_path', lambda: tmp_path / 'missing.toml')

    def install(fetch):
        clock, calls, responses, paces = [1000.0], [], [], []

        def open_request(request, *, timeout):
            calls.append((request, timeout))
            body = fetch(request)
            response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
            responses.append(response)
            return response

        iao = access.DiscogsIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=lambda interval: paces.append(interval)))
        monkeypatch.setattr(discogs, '_iao', iao)
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, paces=paces)
    return install


def test_search_needs_token_but_linked_facts_are_public(boundary, store):
    b = boundary(lambda request: release())
    assert not discogs.available(store)
    with net.probe_errors() as errors, pytest.raises(IntegrationUnavailable):
        discogs.search_releases('Artist Song', store=store)
    assert not b.calls and len(errors) == 1 and not net.provider_outage(errors[0])
    assert discogs.release_details(123, store=store) == release()
    assert b.paces == [2.5]
    assert 'token=' not in b.calls[0][0].full_url
    store.settings.set_setting('discogs_token', 'secret')
    assert discogs.available(store)


@pytest.mark.parametrize('found', [True, False])
def test_search_cache_is_detached_shared_and_expires_after_completion(boundary, store, found):
    def fetch(request):
        b.clock[0] += 20
        return hits(found=found)
    b = boundary(fetch)
    store.settings.set_setting('discogs_token', 'secret')
    result = discogs.search_releases('Artist Song', store=store)
    result.clear()
    ttl = access.POSITIVE_TTL if found else access.MISS_TTL
    b.clock[0] = 1020 + ttl - 1
    assert discogs.search_releases('Artist Song', store=store) == hits(found=found)['results']
    assert len(b.calls) == 1
    b.clock[0] += 1
    assert discogs.search_releases('Artist Song', store=store) == hits(found=found)['results']
    assert len(b.calls) == 2 and b.paces == [1.1, 1.1]
    assert all(response.closed for response in b.responses)
    request, timeout = b.calls[0]
    assert timeout == access.TIMEOUT_S
    assert request.get_header('User-agent') == access.USER_AGENT
    assert request.get_header('Accept') == 'application/json'
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)['token'] == ['secret']


def test_public_cache_excludes_personal_data_and_survives_token_removal(boundary, store):
    b = boundary(lambda request: {**release(), 'user_data': {'in_collection': True}})
    store.settings.set_setting('discogs_token', 'secret')
    assert discogs.release_details(123, store=store) == release()
    store.settings.set_setting('discogs_token', '')
    assert discogs.release_details(123, store=store) == release()
    assert len(b.calls) == 1 and 'secret' not in repr(b.iao._cache)


@pytest.mark.parametrize('failure', [TimeoutError(), http(503), http(429)])
def test_outage_defers_every_caller_until_recovery(boundary, store, failure):
    def fetch(request):
        if len(b.calls) == 1:
            raise failure
        return release()
    b = boundary(fetch)
    store.settings.set_setting('discogs_token', 'secret')
    with net.probe_errors() as errors:
        assert not discogs.probe({'title': 'Song', 'artist': 'Artist'}, store).fields
        with pytest.raises(IntegrationUnavailable):
            discogs.master_details(321, store=store)
        with pytest.raises(IntegrationUnavailable):
            discogs.release_details(123, store=store)
    assert len(b.calls) == 1 and len(errors) == 3
    assert all(error.retry_at == 1030 and net.provider_outage(error) for error in errors)
    assert store.enrichment_state.integration_cooldown('discogs')['retry_at'] == 1030
    discogs.reset()
    assert discogs.tripped()
    b.clock[0] = 1030
    assert discogs.release_details(123, store=store) == release()
    assert len(b.calls) == 2 and not discogs.tripped()
    assert store.enrichment_state.integration_cooldown('discogs') is None


@pytest.mark.parametrize('header,expected', [
    ('7200', 8200),
    (format_datetime(datetime.fromtimestamp(1600, tz=timezone.utc), usegmt=True), 1600),
    ('invalid', 1030), ('', 1030), ('-5', 1030), ('inf', 1030),
])
def test_retry_after_is_shared_and_survives_restart(boundary, store, header, expected):
    def fetch(request):
        raise http(429, {'Retry-After': header})
    b = boundary(fetch)
    with pytest.raises(IntegrationUnavailable) as first:
        discogs.release_details(123, store=store)
    assert first.value.retry_at == expected
    recreated = access.DiscogsIAO(now_fn=lambda: b.clock[0],
        open_fn=lambda *args, **kwargs: pytest.fail('Persisted cooldown was bypassed'),
        pacer=SimpleNamespace(wait=lambda *args: pytest.fail('Deferred work should not pace')))
    with recreated.context(store), pytest.raises(IntegrationUnavailable) as deferred:
        recreated.master(321)
    assert deferred.value.retry_at == expected


@pytest.mark.parametrize('code', [401, 403])
def test_bad_token_blocks_search_but_public_reads_and_replacement_token_work(boundary, store, code):
    def fetch(request):
        if len(b.calls) == 1:
            raise http(code)
        return release() if '/releases/' in request.full_url else hits()
    b = boundary(fetch)
    store.settings.set_setting('discogs_token', 'secret')
    with net.probe_errors() as errors:
        with pytest.raises(IntegrationUnavailable):
            discogs.search_releases('First', store=store)
        with pytest.raises(IntegrationUnavailable):
            discogs.search_releases('Other', store=store)
        assert discogs.release_details(123, store=store) == release()
        assert 'token=' not in b.calls[-1][0].full_url
        store.settings.set_setting('discogs_token', 'replacement')
        assert discogs.search_releases('Other', store=store, tok='secret') == hits()['results']
    assert b.paces == [1.1, 2.5, 1.1] and len(errors) == 2
    assert not any(net.provider_outage(error) for error in errors)
    assert store.enrichment_state.integration_cooldown('discogs') is None
    assert 'token=replacement' in b.calls[-1][0].full_url
    assert 'secret' not in repr(b.iao._cache) + repr(b.iao._waits)


def test_token_edits_invalidate_search_cache_and_missing_token_never_uses_old_argument(boundary, store):
    b = boundary(lambda request: hits())
    config = [{'name': 'discogs', 'enabled': True}]
    previous = enrichment.provider_configuration(store, {'discogs': discogs}, config)
    assert previous['discogs']['status'] == 'unavailable'
    for value in ('first', 'second'):
        store.settings.set_setting('discogs_token', value)
        current = enrichment.provider_configuration(store, {'discogs': discogs}, config)
        assert current['discogs']['fingerprint'] != previous['discogs']['fingerprint']
        assert value not in repr(current)
        previous = current
        assert discogs.search_releases('Song', store=store, tok='obsolete') == hits()['results']
    store.settings.set_setting('discogs_token', '')
    with pytest.raises(IntegrationUnavailable):
        discogs.search_releases('Song', store=store, tok='first')
    assert len(b.calls) == 2


def test_credential_change_during_pacing_is_checked_before_http(boundary, store):
    b = boundary(lambda request: hits())
    store.settings.set_setting('discogs_token', 'old')
    b.iao.pacer.wait = lambda interval: store.settings.set_setting('discogs_token', 'new')
    assert discogs.search_releases('Song', store=store) == hits()['results']
    assert len(b.calls) == 1 and 'token=new' in b.calls[0][0].full_url


def test_stale_good_facts_survive_failure_and_recover_without_negative_cache(boundary):
    def fetch(request):
        if len(b.calls) == 2:
            raise http(503)
        return release()
    b = boundary(fetch)
    assert discogs.release_details(123) == release()
    b.clock[0] += access.POSITIVE_TTL
    with net.probe_errors() as errors:
        assert discogs.release_details(123) == release()
        assert discogs.release_details(123) == release()
    assert len(b.calls) == 2 and len(errors) == 2
    assert errors[0].retry_at == errors[1].retry_at == b.clock[0] + 30
    b.clock[0] += 30
    with net.probe_errors() as errors:
        assert discogs.release_details(123) == release()
    assert not errors and len(b.calls) == 3


@pytest.mark.parametrize('body', [b'not json', [], {}, {'results': [{}]},
    {'results': [{'id': 1, 'title': 'Artist - Song', 'style': 'House'}]}])
def test_malformed_search_is_pending_and_does_not_block_unrelated_public_facts(boundary, body):
    b = boundary(lambda request: body if '/database/search' in request.full_url else release())
    with pytest.raises(IntegrationUnavailable) as failed:
        discogs.search_releases('Song', tok='secret')
    assert not net.provider_outage(failed.value) and not b.iao._cache
    assert discogs.release_details(123) == release()


@pytest.mark.parametrize('change', [
    {'id': 999}, {'title': []}, {'artists': []}, {'artists': [None]}, {'artists': [{'name': []}]},
    {'labels': {}}, {'tracklist': [{'sub_tracks': 'bad'}]},
    {'community': {'rating': []}}, {'notes': []},
])
def test_malformed_detail_is_not_cached_as_a_confirmed_miss(boundary, change):
    b = boundary(lambda request: {**release(), **change})
    with pytest.raises(IntegrationUnavailable):
        discogs.release_details(123)
    assert not b.iao._cache


def test_only_public_404_is_a_cached_miss(boundary):
    def fetch(request):
        raise http(404)
    b = boundary(fetch)
    assert discogs.release_details(123) == {}
    assert discogs.release_details(123) == {}
    assert len(b.calls) == 1
    b.clock[0] += access.MISS_TTL
    assert discogs.release_details(123) == {}
    assert len(b.calls) == 2
    with pytest.raises(IntegrationUnavailable):
        discogs.search_releases('Song', tok='secret')


@pytest.mark.parametrize('failure', [WorkClosing(), WorkBudgetExceeded()])
def test_cancellation_does_not_poison_cache_or_admission(boundary, failure):
    def fetch(request):
        raise failure
    b = boundary(fetch)
    with net.probe_errors() as errors, pytest.raises(type(failure)):
        discogs.release_details(123)
    assert not errors and not b.iao._cache and not b.iao._waits
    assert b.iao.availability().ready(b.clock[0])


def test_overlapping_callers_share_one_request_without_holding_database_lock(boundary, store):
    entered, release_request = Event(), Event()

    def fetch(request):
        assert not store._lock.held_by_current_thread
        entered.set()
        assert release_request.wait(5)
        return release()
    b = boundary(fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(discogs.release_details, 123, store=store)
        try:
            assert entered.wait(5)
            second = pool.submit(discogs.release_details, 123, store=store)
            pool.submit(store.settings.set_setting, 'unrelated', 'updated').result(2)
        finally:
            release_request.set()
        assert first.result(5) == second.result(5) == release()
    assert len(b.calls) == len(b.paces) == 1


def test_bounded_cache_retains_recent_public_records(boundary, monkeypatch):
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)
    b = boundary(lambda request: release(int(urllib.parse.urlsplit(request.full_url).path.rsplit('/', 1)[1])))
    for identity in (1, 2, 1, 3, 1, 2):
        discogs.release_details(identity)
    assert len(b.calls) == 4 and len(b.iao._cache) == 2


def test_search_cache_separates_queries_and_preserves_the_callers_parameters(boundary):
    b = boundary(lambda request: hits())
    params = {'q': 'First', 'type': 'release', 'token': 'obsolete'}
    assert b.iao.get('database/search', params, 'secret') == hits()
    assert params == {'q': 'First', 'type': 'release', 'token': 'obsolete'}
    assert 'token=secret' in b.calls[0][0].full_url
    assert 'obsolete' not in b.calls[0][0].full_url
    b.iao.search({'q': 'Other', 'type': 'release'}, tok='secret')
    b.iao.search({'type': 'release', 'q': 'First'}, tok='secret')
    assert len(b.calls) == 2


def test_success_cannot_clear_a_newer_persisted_cooldown(boundary, store):
    def fetch(request):
        store.enrichment_state.defer_integration('discogs', now=1000,
            retry_after=2000, error='Another process saw throttling')
        return release()
    boundary(fetch)
    assert discogs.release_details(123, store=store) == release()
    assert store.enrichment_state.integration_cooldown('discogs')['retry_at'] == 2000
    with pytest.raises(IntegrationUnavailable) as deferred:
        discogs.master_details(321, store=store)
    assert deferred.value.retry_at == 2000


def test_album_caller_retains_verified_rating_but_reports_stale_evidence_pending(boundary, store, monkeypatch):
    mbid = '12345678-1234-1234-1234-123456789abc'
    mb = {'id': mbid, 'title': 'Album', 'artist-credit': [{'name': 'Artist'}],
          'relations': [{'type': 'discogs', 'url': {'resource': 'https://www.discogs.com/master/321'}}]}
    monkeypatch.setattr(musicbrainz, '_get', lambda path, params, **kwargs:
                        {'release-groups': [mb]} if path == 'release-group' else mb)

    def fetch(request):
        if b.clock[0] > 1000:
            raise http(429, {'Retry-After': '7200'})
        return {**release(321), 'main_release': 123} if '/masters/' in request.full_url else release()
    b = boundary(fetch)
    album = {'artist': 'Artist', 'title': 'Album'}
    first = release_notes.fetch(album, store)
    assert not first['retry'] and first['rating']['value'] == 4.5 and len(b.calls) == 2
    b.clock[0] += access.POSITIVE_TTL
    later = release_notes.fetch(album, store)
    assert later['retry'] and later['rating'] == first['rating']
    assert later['retry_at'] == b.clock[0] + 7200 and len(b.calls) == 3


def test_waterfall_keeps_discogs_pending_and_runs_healthy_provider(boundary, store):
    def fetch(request):
        raise http(503, {'Retry-After': '120'})
    b = boundary(fetch)
    store.settings.set_setting('discogs_token', 'secret')
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
        [{'name': 'discogs', 'enabled': True}, {'name': 'deezer', 'enabled': True}], events.append,
        registry={'discogs': discogs, 'deezer': healthy}, now_fn=lambda: b.clock[0])
    assert not completed and len(b.calls) == 1 and len(healthy_calls) == 4
    attempt = store.enrichment_state.enrichment_attempts('song0|artist')['discogs']
    assert attempt['status'] == 'retry'
    assert store.enrichment_state.integration_cooldown('discogs')['retry_at'] == 1120
    assert all(row['year'] == '1996' for row in store.charts.playlist_tracks_detail(playlist))
    assert any(event.get('warning') for event in events)
