"""All vendor callers share admission, while independent work can still progress."""
import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.integrations.musicbrainz import MusicBrainzIAO, TIMEOUT_S, USER_AGENT
from yt_playlist.providers import musicbrainz, acousticbrainz, release_notes, discogs
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.util import net


def recording(**changes):
    return {'id': 'recording', 'title': 'Song', 'artist-credit': [{'name': 'Artist'}], **changes}


def install(monkeypatch, fetch, clock):
    def open_request(request, **kwargs):
        return io.StringIO(json.dumps(fetch(request, **kwargs)))
    iao = MusicBrainzIAO(open_fn=open_request, now_fn=lambda: clock[0],
        monotonic_fn=lambda: clock[0], pacer=SimpleNamespace(wait=lambda: None))
    monkeypatch.setattr(musicbrainz, '_iao', iao)
    return iao


@pytest.mark.parametrize('header,deadline', [
    ('120', 1120), ('Thu, 01 Jan 1970 00:18:40 GMT', 1120),
    ('7200', 8200), (None, 1030), ('nonsense', 1030), ('inf', 1030), ('nan', 1030),
])
def test_all_callers_share_persisted_cooldown_and_server_deadline(store, monkeypatch, header, deadline):
    clock, calls = [1000.0], []
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        assert request.get_header('User-agent') == USER_AGENT
        assert request.get_header('Accept') == 'application/json'
        assert kwargs['timeout'] == TIMEOUT_S
        raise urllib.error.HTTPError(request.full_url, 503, 'busy',
                                      {'Retry-After': header} if header is not None else {}, None)
    iao = install(monkeypatch, fetch, clock)
    monkeypatch.setattr(discogs, 'token', lambda store: None)
    assert musicbrainz.probe({'title': 'Song (Remaster)', 'artist': 'Artist'}, store).fields == {}
    assert musicbrainz.tag_artists('rock', store=store) == []
    assert release_notes.fetch({'title': 'Album', 'artist': 'Artist'}, store)['retry']
    with pytest.raises(IntegrationUnavailable) as caught:
        acousticbrainz.probe({'title': 'Song', 'artist': 'Artist'}, store)
    assert caught.value.provider == 'musicbrainz'
    assert caught.value.retry_at == deadline
    assert len(calls) == 1  # Includes the stripped-title alternative and the dependency.
    saved = store.enrichment_state.integration_cooldown('musicbrainz')
    assert saved['retry_at'] == deadline and saved['failures'] == 1
    # New object/process state must consult the same durable deadline.
    restarted = MusicBrainzIAO(open_fn=lambda *a, **k: pytest.fail('request during cooldown'),
        now_fn=lambda: clock[0], pacer=SimpleNamespace(wait=lambda: None))
    with restarted.context(store.enrichment_state), pytest.raises(IntegrationUnavailable):
        restarted.get('artist', {'query': 'other'})


def test_concurrent_different_queries_cannot_bypass_the_first_outage(store, monkeypatch):
    calls, clock = [], [1000.0]
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        raise TimeoutError('stalled')
    install(monkeypatch, fetch, clock)
    def read(index):
        with net.probe_errors() as errors:
            musicbrainz.probe({'title': f'Song {index} (Remaster)', 'artist': 'Artist'}, store)
        return errors
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(8)))
    assert len(calls) == 1
    assert all(len(errors) == 1 and errors[0].retry_at == 1030 for errors in results)
    assert store.enrichment_state.integration_cooldown('musicbrainz')['failures'] == 1


def test_successful_search_responses_are_shared_but_detached_and_exact(monkeypatch):
    calls, clock = [], [1000.0]
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return {'recordings': [{'id': 'right', 'title': 'Song', 'artist-credit': [{'name': 'Artist'}]}]}
    iao = install(monkeypatch, fetch, clock)
    result = iao.get('recording', {'query': 'Song', 'limit': '25'})
    result['recordings'][0]['id'] = 'mutated'
    assert iao.get('recording', {'limit': '25', 'query': 'Song'})['recordings'][0]['id'] == 'right'
    iao.get('recording', {'query': 'Song (Live)', 'limit': '25'})
    iao.get('recording', {'query': 'Song', 'limit': '5'})
    assert len(calls) == 3


def test_detail_failure_keeps_search_fields_without_artist_request(store, monkeypatch):
    calls, clock = [], [1000.0]
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            return {'recordings': [{'id': 'right', 'title': 'Song', 'first-release-date': '1995',
                'artist-credit': [{'artist': {'id': 'artist', 'name': 'Artist'}}],
                'releases': [{'id': 'release'}]}]}
        raise TimeoutError('stalled detail')
    install(monkeypatch, fetch, clock)
    with net.probe_errors() as errors:
        result = musicbrainz.probe({'title': 'Song', 'artist': 'Artist'}, store)
    assert result.fields == {'year': '1995', 'mb_recording_id': 'right', 'mb_release_ids': ['release']}
    assert len(calls) == 2 and len(errors) == 1


def test_mb_dependency_outage_keeps_known_mbid_and_other_providers_usable(store, monkeypatch):
    clock, calls, acoustic_calls, healthy_calls = [1000.0], [], [], []
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 503, 'busy', {'Retry-After': '120'}, None)
    install(monkeypatch, fetch, clock)
    acousticbrainz.reset()
    def acoustic(url):
        acoustic_calls.append(url)
        return {'rhythm': {'bpm': 123}} if url.endswith('low-level') else {}
    monkeypatch.setattr(acousticbrainz, '_get_json', acoustic)
    class Healthy:
        name = 'deezer'
        def available(self, store): return True
        def reset(self): pass
        def tripped(self): return False
        def probe(self, track, store):
            healthy_calls.append(track['title'])
            return EnrichmentResult(self.name, {'genre': 'Rock'})
    healthy = Healthy()
    tracks = []
    for index in range(2):
        tid = store.tracks.upsert_track(f'v{index}', f'Song {index}', 'Artist', None, 200)
        if index:
            store.enrichment.apply_provider_fields(tid, 'musicbrainz', {'mb_recording_id': 'known'})
        tracks.append(store.tracks.track_for_waterfall(tid))
    providers = [musicbrainz, acousticbrainz, healthy]
    config = [{'name': p.name, 'enabled': True} for p in providers]
    run_waterfall(store, tracks, config, lambda e: None,
                  registry={p.name: p for p in providers}, now_fn=lambda: clock[0])
    assert len(calls) == 1 and len(acoustic_calls) == 2
    assert healthy_calls == ['Song 0', 'Song 1']
    cooldowns = store.enrichment_state.enrichment_cooldowns()
    assert set(cooldowns) == {'musicbrainz'} and cooldowns['musicbrainz']['failures'] == 1
    unknown = store.enrichment_state.enrichment_provider_state(tracks[0]['recording_id'])
    known = store.enrichment_state.enrichment_provider_state(tracks[1]['recording_id'])
    assert unknown['musicbrainz']['status'] == unknown['acousticbrainz']['status'] == 'retry'
    assert unknown['acousticbrainz']['retry_at'] == 1120
    assert known['acousticbrainz']['status'] == 'found'
    assert known['acousticbrainz']['fields']['bpm'] == 123


def test_success_from_cache_does_not_clear_a_different_outage(store, monkeypatch):
    clock = [1000.0]
    def fetch(request, **kwargs):
        if 'query=good' in request.full_url:
            return {'recordings': []}
        raise TimeoutError('stalled')
    iao = install(monkeypatch, fetch, clock)
    with iao.context(store.enrichment_state):
        iao.get('recording', {'query': 'good'})
        with pytest.raises(IntegrationUnavailable):
            iao.get('recording', {'query': 'bad'})
        assert iao.get('recording', {'query': 'good'}) == {'recordings': []}
    assert store.enrichment_state.integration_cooldown('musicbrainz')['retry_at'] == 1030


def test_recovery_clears_only_its_admitted_cooldown(store, monkeypatch):
    clock, calls = [1000.0], []
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise TimeoutError('stalled')
        return {'recordings': []}
    iao = install(monkeypatch, fetch, clock)
    with iao.context(store.enrichment_state):
        with pytest.raises(IntegrationUnavailable):
            iao.get('recording', {'query': 'Song'})
        clock[0] = 1030
        iao.get('recording', {'query': 'Song'})
        assert store.enrichment_state.integration_cooldown('musicbrainz') is None
    store.enrichment_state.defer_integration('musicbrainz', now=1040, retry_after=1100, error='new outage')
    store.enrichment_state.clear_integration_cooldown('musicbrainz', 1030)
    assert store.enrichment_state.integration_cooldown('musicbrainz')['retry_at'] == 1100


@pytest.mark.parametrize('exception_name', ['WorkClosing', 'WorkBudgetExceeded'])
def test_cancellation_does_not_become_a_completed_miss(store, monkeypatch, exception_name):
    from yt_playlist.core import activity
    from yt_playlist.integrations import musicbrainz as access
    exception_type = getattr(activity, exception_name)
    install(monkeypatch, lambda *a, **k: pytest.fail('cancelled request reached vendor'), [1000.0])
    def cancel():
        raise exception_type('controlled cancellation')
    monkeypatch.setattr(access, 'check_cancellable_work', cancel)
    tid = store.tracks.upsert_track('cancelled', 'Song (Remaster)', 'Artist', None, 200)
    track = store.tracks.track_for_waterfall(tid)
    with pytest.raises(exception_type):
        run_waterfall(store, [track], [{'name': 'musicbrainz', 'enabled': True}], lambda e: None,
                      registry={'musicbrainz': musicbrainz}, now_fn=lambda: 1000)
    assert store.enrichment_state.enrichment_provider_state(track['recording_id']) == {}
    assert store.enrichment_state.enrichment_cooldowns() == {}


@pytest.mark.parametrize('payload', [
    {}, {'unexpected': []}, {'error': 'temporary backend failure'}, {'error': None},
    {'recordings': None}, {'recordings': {}}, {'recordings': ['bad']},
    {'recordings': [recording(**{'artist-credit': ['Artist', 7]})]},
    {'recordings': [recording(**{'artist-credit': None})]},
    {'recordings': [recording(**{'artist-credit': [{'artist': {'name': []}}]})]},
    {'recordings': [recording(releases=[{'date': 2000}])]},
    {'recordings': [recording(tags=[{'name': 'rock', 'count': '--1'}])]},
    {'recordings': [recording(id='../../unexpected')]},
    {'recordings': [{'id': 'recording'}]},
])
def test_invalid_search_stays_pending_without_caching_a_miss(store, monkeypatch, payload):
    calls, clock = [], [1000.0]

    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return payload

    iao = install(monkeypatch, fetch, clock)
    tid = store.tracks.upsert_track('invalid', 'Song (Remaster)', 'Artist', None, 200)
    item = store.tracks.track_for_waterfall(tid)
    completed = run_waterfall(store, [item], [{'name': 'musicbrainz', 'enabled': True}],
        lambda event: None, registry={'musicbrainz': musicbrainz}, now_fn=lambda: clock[0])
    state = store.enrichment_state.enrichment_provider_state(item['recording_id'])['musicbrainz']
    assert state['status'] == 'retry' and state['retry_at'] == 1030
    assert not completed and not iao._cache and len(calls) == 1  # No stripped-title fallback.
    assert store.enrichment_state.integration_cooldown('musicbrainz') is None


@pytest.mark.parametrize('path,good', [
    ('recording', {'recordings': [recording()]}),
    ('artist', {'artists': [{'id': 'artist', 'name': 'Artist'}]}),
    ('release-group', {'release-groups': [recording(id='release-group', title='Album')]}),
    ('recording/recording', recording(tags=[{'name': 'Rock', 'count': '2'}])),
    ('artist/artist', {'id': 'artist', 'name': 'Artist', 'genres': [{'name': 'Rock'}]}),
    ('release-group/release-group', recording(id='release-group', title='Album', relations=[])),
])
def test_malformed_refresh_keeps_last_good_for_every_supported_endpoint(monkeypatch, path, good):
    from yt_playlist.integrations.musicbrainz import POSITIVE_TTL
    calls, clock, invalid = [], [1000.0], [False]

    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return {'unexpected': []} if invalid[0] else good

    iao = install(monkeypatch, fetch, clock)
    params = {'fmt': 'json', 'query': 'Song'} if '/' not in path else {'fmt': 'json'}
    assert iao.get(path, params) == good
    clock[0] += POSITIVE_TTL
    invalid[0] = True
    with net.probe_errors() as errors:
        assert iao.get(path, params) == good
        assert iao.get(path, params) == good
    assert len(calls) == 2 and len(errors) == 2
    assert all(error.provider == 'musicbrainz' and not error.outage
               and error.retry_at == clock[0] + 30 for error in errors)
    invalid[0] = False
    clock[0] += 30
    with net.probe_errors() as recovered:
        assert iao.get(path, params) == good
    assert not recovered and len(calls) == 3


@pytest.mark.parametrize('path,payload', [
    ('artist', {'artists': [{'id': 'artist', 'name': []}]}),
    ('release-group', {'release-groups': [recording(**{'artist-credit': {}})]}),
    ('recording/recording', recording(id=None)),
    ('recording/recording', recording(isrcs=[{}])),
    ('recording/recording', recording(genres=None)),
    ('recording/recording', recording(releases=['bad'])),
    ('artist/artist', {'id': 'artist', 'name': 'Artist', 'aliases': ['bad']}),
    ('artist/artist', {'id': 'artist', 'name': 'Artist', 'tags': [{'name': {}}]}),
    ('release-group/release-group', recording(id='release-group', relations=[{'url': []}])),
])
def test_malformed_nested_shapes_defer_only_the_affected_request(monkeypatch, path, payload):
    calls, clock = [], [1000.0]

    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return {'recordings': []} if 'query=healthy' in request.full_url else payload

    iao = install(monkeypatch, fetch, clock)
    with net.probe_errors() as errors:
        for _ in range(2):
            with pytest.raises(IntegrationUnavailable):
                iao.get(path, {'fmt': 'json'})
        assert iao.get('recording', {'query': 'healthy', 'fmt': 'json'}) == {'recordings': []}
    assert len(errors) == 2 and all(not error.outage for error in errors)
    assert len(calls) == 2


@pytest.mark.parametrize('path,field', [('recording', 'recordings'), ('artist', 'artists'),
                                     ('release-group', 'release-groups')])
def test_explicit_empty_search_is_a_confirmed_cached_miss(monkeypatch, path, field):
    calls = []
    iao = install(monkeypatch, lambda request, **kwargs: calls.append(request) or {field: []}, [1000.0])
    with net.probe_errors() as errors:
        assert iao.get(path, {'query': 'missing'}) == {field: []}
        assert iao.get(path, {'query': 'missing'}) == {field: []}
    assert not errors and len(calls) == 1


def test_default_transport_resolves_installed_wrapper_at_request_time(monkeypatch):
    from yt_playlist.integrations import musicbrainz as access
    iao = MusicBrainzIAO(pacer=SimpleNamespace(wait=lambda: None))
    calls, responses = [], []

    def open_request(request, **kwargs):
        calls.append(request)
        response = io.StringIO('{"recordings":[]}')
        responses.append(response)
        return response

    monkeypatch.setattr(access.urllib.request, 'urlopen', open_request)
    assert iao.get('recording', {'query': 'Song', 'fmt': 'json'}) == {'recordings': []}
    assert len(calls) == 1 and responses[0].closed


@pytest.mark.parametrize('code', [404, 429, 503])
def test_http_error_bodies_close_even_for_confirmed_missing_entities(monkeypatch, code):
    body = io.BytesIO(b'controlled HTTP failure')

    def fetch(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, code, 'controlled error', {}, body)

    iao = install(monkeypatch, fetch, [1000.0])
    with pytest.raises(urllib.error.HTTPError if code == 404 else IntegrationUnavailable):
        iao.get('recording/recording', {'fmt': 'json'})
    assert body.closed


def test_request_specific_waits_remain_bounded(monkeypatch):
    from yt_playlist.integrations import musicbrainz as access
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)
    calls = []
    iao = install(monkeypatch, lambda request, **kwargs: calls.append(request) or {}, [1000.0])
    for title in ('A', 'B', 'C', 'B'):
        with pytest.raises(IntegrationUnavailable):
            iao.get('recording', {'query': title})
    assert len(calls) == 3 and len(iao._waits) == 2 and not iao._cache


def test_empty_artist_entity_has_a_short_miss_ttl(monkeypatch):
    from yt_playlist.integrations.musicbrainz import MISS_TTL
    clock, calls = [1000.0], []
    data = {'id': 'artist', 'name': 'Artist', 'genres': [], 'tags': []}
    iao = install(monkeypatch, lambda request, **kwargs: calls.append(request) or data, clock)
    params = {'inc': 'genres+tags', 'fmt': 'json'}
    assert iao.get('artist/artist', params) == data
    clock[0] += MISS_TTL - 1
    assert iao.get('artist/artist', params) == data
    clock[0] += 1
    assert iao.get('artist/artist', params) == data
    assert len(calls) == 2


def test_merged_mbid_can_resolve_to_a_canonical_entity(monkeypatch):
    good = recording(id='canonical-recording')
    iao = install(monkeypatch, lambda request, **kwargs: good, [1000.0])
    with net.probe_errors() as errors:
        assert iao.get('recording/merged-recording', {'fmt': 'json'}) == good
    assert not errors
