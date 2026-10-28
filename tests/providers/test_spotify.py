"""Optional report artwork through actual Spotify access and durable admission."""
import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.integrations import spotify as transport
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.library.spotify_library import load_spotify_library
from yt_playlist.providers import spotify
from yt_playlist.util import net
from yt_playlist.web.app import create_app

ALBUM = 'https://open.spotify.com/album/abc123'
IMAGE = 'https://i.scdn.co/image/cover123'


def oembed(url=IMAGE):
    return {'type': 'rich', 'version': '1.0', 'provider_name': 'Spotify',
            'provider_url': 'https://spotify.com', 'title': 'Record',
            'html': '<iframe></iframe>', 'thumbnail_url': url}


@pytest.fixture
def boundary(store, monkeypatch):
    def install(fetch=lambda request: oembed()):
        clock, calls, responses, events = [1000.0], [], [], []

        def pace():
            assert not store._lock.held_by_current_thread and not store.conn.in_transaction
            events.append('pace')

        def open_request(request, *, timeout):
            assert not store._lock.held_by_current_thread and not store.conn.in_transaction
            calls.append((request, timeout))
            events.append('request')
            body = fetch(request)
            response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
            responses.append(response)
            return response

        iao = transport.SpotifyIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=pace))
        monkeypatch.setattr(spotify, '_iao', iao)
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, events=events)
    return install


@pytest.mark.parametrize('image', [IMAGE, 'https://image-cdn-fa.spotifycdn.com/image/cover123'])
def test_thumbnail_uses_public_oembed_and_current_spotify_cdns(boundary, image, monkeypatch):
    monkeypatch.setenv('SPOTIFY_ACCESS_TOKEN', 'SECRET')
    b = boundary(lambda request: oembed(image))
    assert spotify.thumbnail(ALBUM + '?si=one') == image
    assert spotify.thumbnail(ALBUM + '?si=two#shared') == image
    assert len(b.calls) == 1 and b.events == ['pace', 'request']
    request, timeout = b.calls[0]
    assert request.full_url == ('https://open.spotify.com/oembed?'
                                'url=https%3A%2F%2Fopen.spotify.com%2Falbum%2Fabc123')
    assert request.get_method() == 'GET' and request.data is None
    assert request.get_header('Authorization') is None and timeout == 8
    assert request.get_header('User-agent') == transport.USER_AGENT
    assert request.get_header('Accept') == 'application/json' and b.responses[0].closed
    assert transport.SpotifyIAO().pacer.min_interval == .2


@pytest.mark.parametrize('url', [None, 123, b'https://open.spotify.com/album/a',
    'https://open.spotify.com/search/album', 'http://open.spotify.com/album/abc123',
    'https://open.spotify.com.evil.example/album/abc123',
    'https://user@open.spotify.com/album/abc123', 'https://open.spotify.com:443/album/abc123',
    'https://open.spotify.com/album/', 'https://open.spotify.com/album/abc/extra',
    'https://open.spotify.com/album/%61bc', 'https://open.spotify.com/album/café',
    'https://[invalid/album/abc', 'https://open.spotify.com/album/' + 'a' * 2048])
def test_invalid_or_non_album_url_never_reaches_vendor(boundary, url):
    b = boundary(lambda request: pytest.fail('Invalid album reached Spotify'))
    assert spotify.thumbnail(url) is None and not b.calls and not b.events


@pytest.mark.parametrize('image', ['https://evil.example/cover.jpg', 'http://i.scdn.co/a',
    'https://i.scdn.co.evil.example/a', 'https://user@i.scdn.co/a', 'https://i.scdn.co:443/a',
    123, '', [], 'https://[invalid/a'])
def test_invalid_image_is_pending_without_poisoning_other_albums(boundary, store, image):
    b = boundary(lambda request: oembed(image) if 'abc123' in request.full_url else oembed())
    with spotify.access(store), net.probe_errors() as errors:
        assert spotify.thumbnail(ALBUM) is None
        assert spotify.thumbnail(ALBUM) is None
        assert spotify.thumbnail('https://open.spotify.com/album/other') == IMAGE
    assert len(b.calls) == 2 and len(errors) == 2
    assert all(error.provider == 'spotify' and not error.outage and error.retry_at == 1060
               for error in errors)
    assert store.enrichment_state.integration_cooldown('spotify') is None


def test_overlapping_reports_share_one_canonical_request(boundary, store):
    b = boundary()

    def read(index):
        with spotify.access(store):
            return spotify.thumbnail(ALBUM + '?si=' + str(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(read, range(8))) == [IMAGE] * 8
    assert len(b.calls) == 1


@pytest.mark.parametrize('code,headers,deadline', [
    (429, {'Retry-After': '120'}, 1120), (503, {'Retry-After': '7200'}, 8200),
    (503, {'Retry-After': 'Thu, 01 Jan 1970 00:18:40 GMT'}, 1120),
    (503, {'Retry-After': 'NaN'}, 1060), (429, {'Retry-After': 'inf'}, 1060),
])
def test_outages_share_retry_after_and_persist_once(boundary, store, code, headers, deadline, caplog):
    body = io.BytesIO(b'SECRET vendor response')

    def fetch(request):
        raise urllib.error.HTTPError(request.full_url, code, 'SECRET message', headers, body)

    b = boundary(fetch)
    with spotify.access(store), net.probe_errors() as errors:
        assert spotify.thumbnail(ALBUM) is None
        assert spotify.thumbnail('https://open.spotify.com/album/another') is None
        spotify.clear_cache()
        assert spotify.thumbnail(ALBUM) is None
    assert len(b.calls) == 1 and len(errors) == 3 and body.closed
    assert all(error.provider == 'spotify' and error.outage and error.retry_at == deadline
               for error in errors)
    saved = store.enrichment_state.integration_cooldown('spotify')
    assert saved['retry_at'] == deadline and saved['failures'] == 1
    assert 'SECRET' not in saved['error'] and 'SECRET' not in caplog.text
    restarted = transport.SpotifyIAO(now_fn=lambda: 1000,
        open_fn=lambda *a, **kw: pytest.fail('Restart bypassed durable cooldown'),
        pacer=SimpleNamespace(wait=lambda: pytest.fail('Blocked access was paced')))
    with restarted.context(store.enrichment_state), pytest.raises(IntegrationUnavailable) as caught:
        restarted.thumbnail(ALBUM)
    assert caught.value.retry_at == deadline


@pytest.mark.parametrize('failure', ['timeout', 'invalid_json', 'invalid_object', 'missing_type'])
def test_failed_refresh_retains_last_good_and_automatically_recovers(boundary, store, failure):
    failed = [False]

    def fetch(request):
        if failed[0]:
            if failure == 'timeout':
                raise TimeoutError('controlled timeout')
            return {'invalid_json': b'invalid json', 'invalid_object': {},
                    'missing_type': {'thumbnail_url': IMAGE}}[failure]
        return oembed(IMAGE if len(b.calls) == 1 else IMAGE + 'new')

    b = boundary(fetch)
    with spotify.access(store):
        assert spotify.thumbnail(ALBUM) == IMAGE
        b.clock[0] += transport.POSITIVE_TTL
        failed[0] = True
        with net.probe_errors() as errors:
            assert spotify.thumbnail(ALBUM) == IMAGE
            assert spotify.thumbnail(ALBUM + '?si=shared') == IMAGE
        assert len(b.calls) == 2 and len(errors) == 2
        b.clock[0] = errors[0].retry_at
        failed[0] = False
        with net.probe_errors() as recovered:
            assert spotify.thumbnail(ALBUM) == IMAGE + 'new'
        assert not recovered and len(b.calls) == 3
        assert store.enrichment_state.integration_cooldown('spotify') is None


@pytest.mark.parametrize('kind', ['nullable', 404, 410])
def test_confirmed_misses_expire_and_close_http_errors(boundary, kind):
    bodies = []

    def fetch(request):
        if kind != 'nullable':
            body = io.BytesIO(b'missing')
            bodies.append(body)
            raise urllib.error.HTTPError(request.full_url, kind, 'missing', {}, body)
        return oembed(None)

    b = boundary(fetch)
    with net.probe_errors() as errors:
        assert spotify.thumbnail(ALBUM) is None
        b.clock[0] += transport.MISS_TTL - 1
        assert spotify.thumbnail(ALBUM) is None and len(b.calls) == 1
        b.clock[0] += 1
        assert spotify.thumbnail(ALBUM) is None and len(b.calls) == 2
    assert not errors and all(body.closed for body in bodies)


def test_response_and_invalid_response_caches_are_bounded(boundary):
    b = boundary(lambda request: oembed(None))
    for index in range(transport.CACHE_SIZE + 5):
        assert spotify.thumbnail(f'https://open.spotify.com/album/a{index}') is None
    assert len(b.iao._cache) == transport.CACHE_SIZE
    assert 'https://open.spotify.com/album/a0' not in b.iao._cache
    assert spotify.thumbnail('https://open.spotify.com/album/a5') is None
    assert spotify.thumbnail('https://open.spotify.com/album/final') is None
    assert 'https://open.spotify.com/album/a5' in b.iao._cache
    assert 'https://open.spotify.com/album/a6' not in b.iao._cache
    b = boundary(lambda request: {})
    for index in range(transport.CACHE_SIZE + 5):
        assert spotify.thumbnail(f'https://open.spotify.com/album/a{index}') is None
    assert len(b.iao._waits) == transport.CACHE_SIZE and not b.iao._cache


def test_response_size_limit_is_retryable_and_closes_body(boundary):
    b = boundary(lambda request: b' ' * (transport.MAX_RESPONSE_BYTES + 1))
    with net.probe_errors() as errors:
        assert spotify.thumbnail(ALBUM) is None
    assert len(errors) == 1 and errors[0].retry_at == 1060 and not errors[0].outage
    assert b.responses[0].closed and not b.iao._cache


def test_ttl_starts_after_slow_response_completes(boundary):
    def fetch(request):
        b.clock[0] += 8
        return oembed()

    b = boundary(fetch)
    assert spotify.thumbnail(ALBUM) == IMAGE
    b.clock[0] += transport.POSITIVE_TTL - 1
    assert spotify.thumbnail(ALBUM) == IMAGE and len(b.calls) == 1
    b.clock[0] += 1
    assert spotify.thumbnail(ALBUM) == IMAGE and len(b.calls) == 2


@pytest.mark.parametrize('error_type', [WorkClosing, WorkBudgetExceeded])
def test_cancellation_propagates_without_publishing_failure(boundary, store, error_type):
    def fetch(request):
        raise error_type('cancelled')

    b = boundary(fetch)
    with spotify.access(store), net.probe_errors() as errors, pytest.raises(error_type):
        spotify.thumbnail(ALBUM)
    assert not errors and not b.iao._cache and not b.iao._waits
    assert store.enrichment_state.integration_cooldown('spotify') is None


def test_runtime_transport_wrapper_is_not_bypassed(boundary, monkeypatch):
    b = boundary()
    b.iao.open = None
    monkeypatch.setattr(transport.urllib.request, 'urlopen',
                        lambda request, timeout: io.BytesIO(json.dumps(oembed()).encode()))
    assert spotify.thumbnail(ALBUM) == IMAGE


def test_report_and_database_proceed_while_thumbnail_waits_for_http(boundary, store):
    entered, release = Event(), Event()

    def fetch(request):
        entered.set()
        assert release.wait(5), 'Test did not release Spotify request'
        return oembed()

    b = boundary(fetch)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    web = TestClient(app, base_url='http://127.0.0.1:8123')
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(web.get, '/import/spotify-library/spotify-thumbnail',
                              params={'url': ALBUM}, follow_redirects=False)
        try:
            assert entered.wait(3)
            pool.submit(store.settings.set_setting, 'foreground', 'saved').result(2)
            assert not app.state.ctx.spotify_import_lock.locked()
            assert pool.submit(web.get, '/import/spotify-library/status').result(2).status_code == 204
        finally:
            release.set()
        response = pending.result(3)
    assert response.status_code == 307 and response.headers['location'] == IMAGE
    assert response.headers['cache-control'] == 'private, max-age=86400' and len(b.calls) == 1


def test_route_persists_cooldown_and_other_requests_use_it(boundary, store):
    def fetch(request):
        raise OSError('offline')

    b = boundary(fetch)
    web = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False),
                     base_url='http://127.0.0.1:8123')
    for album in (ALBUM, 'https://open.spotify.com/album/other'):
        assert web.get('/import/spotify-library/spotify-thumbnail', params={'url': album},
                       follow_redirects=False).status_code == 404
    assert len(b.calls) == 1
    assert store.enrichment_state.integration_cooldown('spotify')['retry_at'] == 1060


def test_reopened_database_keeps_wait_and_success_cannot_clear_a_newer_deadline(boundary, tmp_path):
    path = tmp_path / 'spotify.db'
    db = Store(path)
    db.init_schema()
    try:
        db.enrichment_state.defer_integration('spotify', now=1000, retry_after=1100, error='old')
    finally:
        db.conn.close()
    db = Store(path)
    db.init_schema()
    try:
        b = boundary()
        with b.iao.context(db.enrichment_state), pytest.raises(IntegrationUnavailable):
            b.iao.thumbnail(ALBUM)
        assert not b.calls
        b.clock[0] = 1100

        def fetch(request):
            db.enrichment_state.defer_integration('spotify', now=1100, retry_after=1300, error='new')
            return oembed()

        b = boundary(fetch)
        b.clock[0] = 1100
        with b.iao.context(db.enrichment_state):
            assert b.iao.thumbnail(ALBUM) == IMAGE
            assert db.enrichment_state.integration_cooldown('spotify')['retry_at'] == 1300
            assert b.iao.thumbnail(ALBUM) == IMAGE
            with pytest.raises(IntegrationUnavailable) as caught:
                b.iao.thumbnail('https://open.spotify.com/album/other')
            assert caught.value.retry_at == 1300
    finally:
        db.conn.close()


def test_scope_is_restored_after_report_access(boundary, store):
    b = boundary()
    with spotify.access(store):
        assert spotify.thumbnail(ALBUM) == IMAGE
    store.enrichment_state.defer_integration('spotify', now=1000, retry_after=1300, error='scoped')
    assert spotify.thumbnail('https://open.spotify.com/album/standalone') == IMAGE
    assert store.enrichment_state.integration_cooldown('spotify')['retry_at'] == 1300


def test_local_account_data_parsing_does_not_access_spotify(boundary):
    import zipfile

    b = boundary(lambda request: pytest.fail('Offline Account Data parsing accessed Spotify'))
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('YourLibrary.json', json.dumps({'albums': [
            {'artist': 'Artist', 'album': 'Record', 'uri': 'spotify:album:abc123'}]}))
    assert load_spotify_library(raw.getvalue())['albums'][0]['title'] == 'Record'
    assert not b.calls
