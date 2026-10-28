"""Shared Wikipedia access through controlled transport and isolated stores."""
import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from threading import Event
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import pytest

from tests.conftest import FakeClient
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations import wikipedia as access
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.providers import wikipedia
from yt_playlist.rec import into_recently, tv_artwork
from yt_playlist.util import net

NOW = 3_000_000.0
ARTIST = 'Robert Hood'
IMAGE = 'https://upload.wikimedia.org/1200px-Robert_Hood.jpg'
FILE = {'query': {'pages': {'1': {'imageinfo': [{
    'thumburl': IMAGE, 'descriptionurl': 'https://commons.wikimedia.org/wiki/File:Robert_Hood.jpg',
    'extmetadata': {'Artist': {'value': '<b>Photographer</b>'},
                    'LicenseShortName': {'value': 'CC BY-SA 4.0'}}}]}}}}


def search_url(artist):
    return access.SEARCH + '?' + urlencode({'action': 'query', 'format': 'json',
        'list': 'search', 'srlimit': '5', 'srsearch': artist + ' band musician'})


def summary_url(artist):
    return access.SUMMARY + quote(artist.replace(' ', '_'), safe='') + '?redirect=true'


def http(code, headers=None):
    return urllib.error.HTTPError(access.SEARCH, code, 'controlled error', headers or {}, None)


def payload(request):
    parts = urlsplit(request.full_url)
    params = parse_qs(parts.query)
    if params.get('list') == ['search']:
        title = params['srsearch'][0].removesuffix(' band musician')
        return {'query': {'search': [{'title': title}]}}
    if '/page/summary/' in parts.path:
        title = unquote(parts.path.rsplit('/', 1)[-1]).replace('_', ' ')
        return {'type': 'standard', 'title': title, 'extract': f'{title} is a musician.',
            'description': 'Electronic musician',
            'content_urls': {'desktop': {'page': 'https://en.wikipedia.org/wiki/' + quote(title)}},
            'originalimage': {'source': 'https://upload.wikimedia.org/Robert_Hood.jpg'}}
    return deepcopy(FILE)


@pytest.fixture
def boundary(monkeypatch):
    def install(fetch=payload, headers=None):
        clock, calls, responses, paces = [NOW], [], [], []

        def open_request(request, *, timeout):
            calls.append((request, timeout))
            data = fetch(request)
            response = io.BytesIO(data if isinstance(data, bytes) else json.dumps(data).encode())
            response.headers = headers or {}
            responses.append(response)
            return response

        iao = access.WikipediaIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=lambda: paces.append(clock[0])))
        monkeypatch.setattr(wikipedia, '_iao', iao)
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, paces=paces)
    return install


def test_summary_and_artwork_share_raw_evidence_without_mixing_their_projection(boundary, store):
    b = boundary()
    with wikipedia.access(store):
        summary = wikipedia.fetch_summary('artist', ARTIST)
        art = wikipedia.fetch_artwork(ARTIST)
    assert summary['title'] == ARTIST and summary['extract'] == f'{ARTIST} is a musician.'
    assert art == {'url': IMAGE, 'source_url': 'https://commons.wikimedia.org/wiki/File:Robert_Hood.jpg',
                   'credit': 'Robert Hood · Photographer · CC BY-SA 4.0', 'kind': 'artist'}
    assert 'extract' not in art and len(b.calls) == len(b.paces) == 3
    assert access.MIN_INTERVAL == .2 and all(response.closed for response in b.responses)
    for request, timeout in b.calls:
        assert timeout == access.TIMEOUT_S == 8
        assert request.get_header('User-agent') == access.USER_AGENT
        assert request.get_header('Accept') == 'application/json'
        assert urlsplit(request.full_url).netloc == 'en.wikipedia.org'
    raw = b.iao.get_url(search_url(ARTIST))
    raw['query']['search'].clear()
    assert b.iao.get_url(search_url(ARTIST))['query']['search'] == [{'title': ARTIST}]
    assert len(b.calls) == 3


@pytest.mark.parametrize('failure', [TimeoutError('offline'), http(503), http(429)])
def test_host_outage_is_shared_across_callers_and_endpoints_until_recovery(boundary, store, failure):
    def fetch(request):
        if len(b.calls) == 1:
            raise failure
        return payload(request)
    b = boundary(fetch)
    with wikipedia.access(store), net.probe_errors() as errors:
        with pytest.raises(wikipedia.LookupUnavailable) as first:
            wikipedia.fetch_summary('artist', ARTIST)
        with pytest.raises(wikipedia.LookupUnavailable):
            wikipedia.fetch_artwork('Other Artist')
        with pytest.raises(IntegrationUnavailable):
            b.iao.get_url(summary_url(ARTIST))
    assert len(b.calls) == 1 and len(errors) == 3
    assert all(error.provider == 'wikipedia' and error.retry_at == NOW + 30 for error in errors)
    assert first.value.retry_at == NOW + 30
    assert store.enrichment_state.integration_cooldown('wikipedia')['retry_at'] == NOW + 30
    b.clock[0] += 30
    with wikipedia.access(store):
        assert wikipedia.fetch_artwork('Other Artist')['url'] == IMAGE
    assert len(b.calls) == 4 and store.enrichment_state.integration_cooldown('wikipedia') is None


@pytest.mark.parametrize('header,delay', [
    ('120', 120),
    (format_datetime(datetime.fromtimestamp(NOW + 600, tz=timezone.utc), usegmt=True), 600),
    ('invalid', 30), ('inf', 30), ('-2', 30),
])
def test_retry_after_survives_recreation(boundary, store, header, delay):
    def fetch(request):
        raise http(429, {'Retry-After': header})
    b = boundary(fetch)
    with wikipedia.access(store), pytest.raises(wikipedia.LookupUnavailable) as first:
        wikipedia.fetch_artwork(ARTIST)
    assert first.value.retry_at == NOW + delay
    recreated = access.WikipediaIAO(now_fn=lambda: b.clock[0],
        open_fn=lambda *a, **kw: pytest.fail('Persisted cooldown was bypassed'),
        pacer=SimpleNamespace(wait=lambda: pytest.fail('Deferred access must not pace')))
    with recreated.context(store), pytest.raises(IntegrationUnavailable) as pending:
        recreated.get_url(summary_url('Other Artist'))
    assert pending.value.retry_at == NOW + delay


@pytest.mark.parametrize('code,outage', [('maxlag', True), ('ratelimited', True),
    ('readonly', True), ('internal_api_error_DBConnectionError', True), ('badvalue', False)])
def test_http_200_api_errors_do_not_become_confirmed_absence(boundary, store, code, outage):
    def fetch(request):
        if len(b.calls) == 1:
            return {'error': {'code': code, 'info': 'controlled failure'}}
        return payload(request)
    b = boundary(fetch, {'Retry-After': '120'})
    with wikipedia.access(store), net.probe_errors() as errors:
        with pytest.raises(wikipedia.LookupUnavailable):
            wikipedia.fetch_summary('artist', ARTIST)
        if outage:
            with pytest.raises(wikipedia.LookupUnavailable):
                wikipedia.fetch_artwork('Other Artist')
        else:
            assert wikipedia.fetch_artwork('Other Artist')['url'] == IMAGE
    assert errors and errors[0].outage == outage and errors[0].retry_at == NOW + 120
    assert len(b.calls) == (1 if outage else 4)


@pytest.mark.parametrize('status', [404, 410])
def test_missing_summary_is_negative_cached_without_suppressing_other_subjects(boundary, status):
    def fetch(request):
        if 'page/summary/Robert_Hood' in request.full_url:
            raise http(status)
        return payload(request)
    b = boundary(fetch)
    with net.probe_errors() as errors:
        assert wikipedia.fetch_summary('artist', ARTIST) is None
        assert wikipedia.fetch_summary('artist', ARTIST) is None
    assert not errors and len(b.calls) == 2
    assert wikipedia.fetch_summary('artist', 'Other Artist')['title'] == 'Other Artist'
    assert len(b.calls) == 4
    b.clock[0] += access.MISS_TTL
    assert wikipedia.fetch_summary('artist', ARTIST) is None and len(b.calls) == 5


def test_search_http_404_is_pending_and_valid_empty_search_is_a_miss(boundary):
    def fetch(request):
        if 'Robert' in request.full_url:
            raise http(404)
        return {'query': {'search': []}}
    b = boundary(fetch)
    with pytest.raises(wikipedia.LookupUnavailable):
        wikipedia.fetch_summary('artist', ARTIST)
    assert wikipedia.fetch_summary('artist', 'Unknown') is None
    assert wikipedia.fetch_summary('artist', 'Unknown') is None and len(b.calls) == 2


@pytest.mark.parametrize('bad', [b'invalid JSON', [], {}, {'query': {'search': None}},
    {'query': {'search': [None]}}, {'query': {'search': [{'title': 123}]}}])
def test_invalid_search_defers_only_its_request(boundary, bad):
    def fetch(request):
        return bad if 'Robert' in request.full_url else payload(request)
    b = boundary(fetch)
    with pytest.raises(wikipedia.LookupUnavailable):
        wikipedia.fetch_summary('artist', ARTIST)
    assert wikipedia.fetch_summary('artist', 'Other Artist')['title'] == 'Other Artist'
    assert b.iao.availability().ready(b.clock[0]) and len(b.calls) == 3


@pytest.mark.parametrize('bad', [{'type': 'error'}, {'type': 'standard'}, {'type': 'standard', 'extract': []},
    {'type': 'standard', 'originalimage': []}, {'type': 'standard', 'content_urls': {'desktop': []}}])
def test_invalid_summary_is_pending_without_discarding_cached_search(boundary, bad):
    def fetch(request):
        return bad if '/page/summary/' in request.full_url else payload(request)
    b = boundary(fetch)
    for _ in range(2):
        with pytest.raises(wikipedia.LookupUnavailable):
            wikipedia.fetch_summary('artist', ARTIST)
    assert len(b.calls) == 2 and not b.iao._availability.retry_at


@pytest.mark.parametrize('bad', [{'query': {'pages': []}}, {'query': {'pages': {}}},
    {'query': {'pages': {'1': {'imageinfo': None}}}},
    {'query': {'pages': {'1': {'imageinfo': [{'extmetadata': []}]}}}},
    {'query': {'pages': {'1': {'imageinfo': [{'extmetadata': {'Artist': {'value': 123}}}]}}}}])
def test_malformed_file_metadata_is_pending(boundary, bad):
    def fetch(request):
        return bad if 'prop=imageinfo' in request.full_url else payload(request)
    b = boundary(fetch)
    for _ in range(2):
        with pytest.raises(wikipedia.LookupUnavailable):
            wikipedia.fetch_artwork(ARTIST)
    assert wikipedia.fetch_summary('artist', ARTIST)['title'] == ARTIST
    assert len(b.calls) == 3


def test_missing_file_is_a_confirmed_miss_and_is_rechecked_after_miss_ttl(boundary):
    def fetch(request):
        return ({'query': {'pages': {'-1': {'title': 'File:Robert_Hood.jpg', 'missing': ''}}}}
                if 'prop=imageinfo' in request.full_url else payload(request))
    b = boundary(fetch)
    with net.probe_errors() as errors:
        assert wikipedia.fetch_artwork(ARTIST) is None
        assert wikipedia.fetch_artwork(ARTIST) is None
    assert not errors and len(b.calls) == 3
    b.clock[0] += access.MISS_TTL
    assert wikipedia.fetch_artwork(ARTIST) is None and len(b.calls) == 4


@pytest.mark.parametrize('changes', [
    {'LicenseShortName': {'value': 'Fair use'}}, {'NonFree': {'value': 'true'}},
    {'Artist': {'value': ''}}, {'LicenseShortName': {'value': ''}},
])
def test_license_and_attribution_checks_still_belong_to_artwork_projection(boundary, changes):
    def fetch(request):
        result = payload(request)
        if 'prop=imageinfo' in request.full_url:
            result['query']['pages']['1']['imageinfo'][0]['extmetadata'].update(changes)
        return result
    b = boundary(fetch)
    with net.probe_errors() as errors:
        assert wikipedia.fetch_artwork(ARTIST) is None
    assert not errors and len(b.calls) == 3


def test_stale_artist_photo_remains_usable_with_truthful_retry_evidence(boundary, store):
    def fetch(request):
        if b.clock[0] > NOW:
            raise http(503, {'Retry-After': '180'})
        return payload(request)
    b = boundary(fetch)
    with wikipedia.access(store):
        first = wikipedia.fetch_artwork(ARTIST)
    b.clock[0] += access.POSITIVE_TTL
    with wikipedia.access(store), net.probe_errors() as errors:
        assert wikipedia.fetch_artwork(ARTIST) == first
    assert len(b.calls) == 4 and len(errors) == 3
    assert all(error.retry_at == b.clock[0] + 180 for error in errors)


def test_stale_story_is_not_restamped_and_tv_demand_waits_for_shared_recovery(boundary, store):
    failed = [False]

    def fetch(request):
        if failed[0]:
            raise http(503, {'Retry-After': '180'})
        return payload(request)
    b = boundary(fetch)
    wikipedia.fetch_artwork(ARTIST)
    b.clock[0] += access.POSITIVE_TTL
    failed[0] = True
    subject = {'subject': 'artist:robert hood', 'kind': 'artist', 'display': ARTIST}
    store.wiki.put(subject['subject'], 'artist', ARTIST,
                   {'title': ARTIST, 'extract': 'Previously prepared.'}, 0)
    into_recently.refresh_summary(store, subject, b.clock[0], wikipedia.fetch_summary)
    row = store.wiki.get(subject['subject'])
    assert row['extract'] == 'Previously prepared.' and row['fetched_at'] == 0
    deadline = b.clock[0] + 180
    assert not store.wiki.should_refresh(subject['subject'], row, deadline - 1)
    store.discovery.upsert_discovered_artist(ARTIST, 1, [], [], None, 1)
    assert tv_artwork.request(store, ARTIST, b.clock[0])
    assert not tv_artwork.prepare(store, FakeClient(), [ARTIST], b.clock[0])
    photos, cached = tv_artwork.images(store, ARTIST)
    assert IMAGE in photos and cached['retry_at'] == deadline
    assert store.rec_proposals.get_proposals(tv_artwork.REQUESTS)
    assert len(b.calls) == 4
    failed[0] = False
    b.clock[0] = deadline
    assert not tv_artwork.prepare(store, FakeClient(), [ARTIST], deadline)
    assert not store.rec_proposals.get_proposals(tv_artwork.REQUESTS)
    assert len(b.calls) == 7
    into_recently.refresh_summary(store, subject, deadline, wikipedia.fetch_summary)
    assert store.wiki.get(subject['subject'])['fetched_at'] == deadline
    assert store.enrichment_state.integration_cooldown('wikipedia') is None


def test_cold_story_failure_does_not_create_a_negative_card(boundary, store):
    def fetch(request):
        raise http(429, {'Retry-After': '180'})
    b = boundary(fetch)
    subject = {'subject': 'artist:robert hood', 'kind': 'artist', 'display': ARTIST}
    into_recently.refresh_summary(store, subject, NOW, wikipedia.fetch_summary)
    assert store.wiki.get(subject['subject']) is None
    assert not store.wiki.should_refresh(subject['subject'], None, NOW + 179)
    assert store.wiki.should_refresh(subject['subject'], None, NOW + 180)
    assert len(b.calls) == 1


def test_album_artwork_retains_last_good_projection_and_honors_vendor_deadline(boundary, store, monkeypatch):
    from yt_playlist.providers import album_editorial, deepseek, release_notes
    from yt_playlist.rec import album_notes
    album = {'browse_id': 'wiki-artist-album', 'artist': ARTIST, 'title': 'Album'}
    old = {'url': 'https://img.example/old.jpg',
           'source_url': 'https://music.youtube.com/channel/UCartist',
           'kind': 'artist', 'credit': ARTIST}
    store.rec_proposals.publish_album_note(album['browse_id'],
        {'editorial_version': 6, 'artwork': old}, NOW - 1)
    monkeypatch.setattr(album_editorial, 'lookup', lambda *a: None)
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *a: None)
    monkeypatch.setattr(release_notes, 'fetch',
        lambda *a: {'description': None, 'evidence': [], 'retry': False})

    def fetch(request):
        raise http(429, {'Retry-After': '7200'})
    b = boundary(fetch)
    album_notes.prepare(store, NOW, [album])
    note = album_notes.read(store, album)
    assert note['artwork'] == old and note['retry_at'] == NOW + 7200
    assert len(b.calls) == 1


def test_album_stale_raw_photo_remains_pending_after_projection(boundary, store, monkeypatch):
    from yt_playlist.providers import album_editorial, deepseek, release_notes
    from yt_playlist.rec import album_notes

    def fetch(request):
        if b.clock[0] > NOW:
            raise http(503, {'Retry-After': '7200'})
        return payload(request)
    b = boundary(fetch)
    art = wikipedia.fetch_artwork(ARTIST)
    b.clock[0] += access.POSITIVE_TTL
    monkeypatch.setattr(album_editorial, 'lookup', lambda *a: None)
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *a: None)
    monkeypatch.setattr(release_notes, 'fetch',
        lambda *a: {'description': None, 'evidence': [], 'retry': False})
    album = {'browse_id': 'raw-photo-album', 'artist': ARTIST, 'title': 'Album'}
    album_notes.prepare(store, b.clock[0], [album])
    note = album_notes.read(store, album)
    assert note['artwork'] == art and note['retry_at'] == b.clock[0] + 7200
    assert len(b.calls) == 4


def test_overlapping_callers_share_requests_without_holding_the_database_lock(boundary, store):
    entered, release = Event(), Event()

    def fetch(request):
        assert not store._lock.held_by_current_thread
        entered.set()
        assert release.wait(5)
        return payload(request)
    b = boundary(fetch)

    def read(artwork):
        with wikipedia.access(store):
            return wikipedia.fetch_artwork(ARTIST) if artwork else wikipedia.fetch_summary('artist', ARTIST)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(read, False)
        try:
            assert entered.wait(5)
            second = pool.submit(read, True)
            pool.submit(store.settings.set_setting, 'unrelated', 'updated').result(2)
        finally:
            release.set()
        assert first.result(5)['title'] == ARTIST and second.result(5)['url'] == IMAGE
    assert len(b.calls) == 3


def test_raw_cache_ttl_starts_after_response_and_cache_and_waits_are_bounded(boundary, monkeypatch):
    def fetch(request):
        b.clock[0] += 20
        return payload(request)
    b = boundary(fetch)
    b.iao.get_url(search_url(ARTIST))
    b.clock[0] = NOW + 20 + access.POSITIVE_TTL - 1
    b.iao.get_url(search_url(ARTIST))
    assert len(b.calls) == 1
    b.clock[0] += 1
    b.iao.get_url(search_url(ARTIST))
    assert len(b.calls) == 2
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)
    for artist in ('One', 'Two', 'One', 'Three'):
        b.iao.get_url(search_url(artist))
    assert list(b.iao._cache) == [('search', 'One band musician', '5'), ('search', 'Three band musician', '5')]
    b.iao.open = lambda *a, **kw: io.BytesIO(b'invalid JSON')
    for artist in ('A', 'B', 'C'):
        with pytest.raises(IntegrationUnavailable):
            b.iao.get_url(search_url(artist))
    assert len(b.iao._waits) == 2


@pytest.mark.parametrize('failure', [WorkClosing(), WorkBudgetExceeded()])
@pytest.mark.parametrize('artwork', [False, True])
def test_cancellation_propagates_without_a_vendor_failure_or_miss(boundary, failure, artwork):
    def fetch(request):
        raise failure
    b = boundary(fetch)
    with net.probe_errors() as errors, pytest.raises(type(failure)):
        wikipedia.fetch_artwork(ARTIST) if artwork else wikipedia.fetch_summary('artist', ARTIST)
    assert not errors and not b.iao._cache and not b.iao._waits


@pytest.mark.parametrize('url', ['http://en.wikipedia.org/w/api.php',
    'https://en.wikipedia.org.evil.example/w/api.php',
    'https://upload.wikimedia.org/photo.jpg', 'https://en.wikipedia.org/wiki/Artist',
    search_url(ARTIST) + '&list=search', summary_url(ARTIST) + '&other=1',
    summary_url(ARTIST) + '#fragment', access.SUMMARY + '..?redirect=true'])
def test_boundary_rejects_unexpected_hosts_and_resources(boundary, url):
    b = boundary()
    with pytest.raises(ValueError):
        b.iao.get_url(url)
    assert not b.calls


def test_success_cannot_clear_a_newer_persisted_cooldown(boundary, store):
    def fetch(request):
        store.enrichment_state.defer_integration('wikipedia', now=NOW,
            retry_after=NOW + 500, error='newer process deadline')
        return payload(request)
    b = boundary(fetch)
    with b.iao.context(store):
        assert b.iao.get_url(search_url(ARTIST))['query']['search']
        assert not b.iao.availability().ready(NOW)
    assert store.enrichment_state.integration_cooldown('wikipedia')['retry_at'] == NOW + 500
