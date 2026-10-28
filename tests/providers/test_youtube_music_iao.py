"""Exercise real SDK/session admission with controlled browser and sprite transports."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests

from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.core.bridge import BridgeError, BridgeReply
from yt_playlist.core.bridge_session import BridgeSession
from yt_playlist.core.identities import build_client
from yt_playlist.integrations import youtube_music as yt

BASE = 'https://music.youtube.com/youtubei/v1/'


class Browser:
    connected = True
    connection_id = 1

    def __init__(self):
        self.calls = []
        self.result = BridgeReply(200, '{}')

    def execute(self, method, url, body):
        self.calls.append((method, url, body))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def access(browser):
    clock = [1000.0]
    return yt.YouTubeMusicIAO(browser, now_fn=lambda: clock[0], min_interval=0), clock


def test_fresh_clients_share_policy_and_keep_brand_context_and_credentials_in_browser():
    browser = Browser()
    main = build_client(SimpleNamespace(brand_account_id=None), browser)
    brand = build_client(SimpleNamespace(brand_account_id='brand-id'), browser)
    assert main._session._access is brand._session._access is yt.for_bridge(browser)
    main._session._access.min_interval = 0
    for client, endpoint in [(main, 'browse'), (brand, 'search'), (brand, 'next'),
                             (main, 'player'), (brand, 'browse/edit_playlist')]:
        assert client._send_request(endpoint, {'videoId': 'test'}) == {}
    assert [body['context'].get('user', {}).get('onBehalfOfUser')
            for _, _, body in browser.calls] == [None, 'brand-id', 'brand-id', None, 'brand-id']
    session = BridgeSession(browser)
    session.post(BASE + 'browse', json={}, headers={'Authorization': 'private-secret'},
                 cookies={'SAPISID': 'private-secret'})
    assert 'private-secret' not in json.dumps(browser.calls)


@pytest.mark.parametrize('endpoint,mutation', [
    ('browse', False), ('search', False), ('next', False), ('player', False),
    ('music/get_search_suggestions', False), ('account/account_menu', False),
    ('browse/edit_playlist', True), ('playlist/create', True), ('playlist/delete', True),
    ('feedback', True), ('like/like', True), ('like/dislike', True), ('like/removelike', True),
    ('subscription/subscribe', True), ('subscription/unsubscribe', True), ('unknown', True),
])
def test_read_write_inventory_is_conservative(endpoint, mutation):
    assert yt.YouTubeMusicIAO.operation('POST', BASE + endpoint) is mutation
    assert yt.YouTubeMusicIAO.operation('POST', BASE + 'browse', {'formData': {}})


@pytest.mark.parametrize('url', [
    'http://music.youtube.com/youtubei/v1/browse',
    'https://evil.example/youtubei/v1/browse',
    'https://music.youtube.com.evil.example/youtubei/v1/browse',
    'https://secret@music.youtube.com/youtubei/v1/browse',
    'https://music.youtube.com:443/youtubei/v1/browse',
    BASE + '../browse', BASE + '%2e%2e/browse', BASE + 'browse#secret',
    'https://music.youtube.com/',
])
def test_vendor_boundary_rejects_unrelated_or_disguised_requests(url):
    browser = Browser()
    iao, _ = access(browser)
    with pytest.raises(ValueError):
        iao.request('POST', url, {})
    assert not browser.calls


@pytest.mark.parametrize('status', [401, 403, 0])
def test_auth_is_identity_scoped_recovers_on_reconnect_and_expires(status):
    browser = Browser()
    iao, clock = access(browser)
    browser.result = BridgeReply(status, json.dumps({'error': 'not signed in'}) if status == 0
                                else '{"error":{"message":"denied"}}')
    expected = status or 401
    assert iao.request('POST', BASE + 'browse', {}, identity='brand').status_code == expected
    browser.result = BridgeReply(200, '{}')
    assert iao.request('POST', BASE + 'browse', {}, identity='brand').status_code == expected
    assert len(browser.calls) == 1
    assert iao.request('POST', BASE + 'browse', {}, identity='main').status_code == 200
    clock[0] += yt.SESSION_RETRY_S + 1
    assert iao.request('POST', BASE + 'browse', {}, identity='brand').status_code == 200
    browser.result = BridgeReply(401, '{"error":{"message":"denied"}}')
    iao.request('POST', BASE + 'browse', {}, identity='brand')
    browser.connection_id += 1
    browser.result = BridgeReply(200, '{}')
    assert iao.request('POST', BASE + 'browse', {}, identity='brand').status_code == 200


@pytest.mark.parametrize('header,deadline', [
    ('120', 1120), ('Thu, 01 Jan 1970 00:20:00 GMT', 1200),
    ('NaN', 1030), ('infinity', 1030), ('invalid', 1030),
])
def test_throttle_is_shared_honors_retry_after_survives_reconnect_and_recovers(header, deadline):
    browser = Browser()
    iao, clock = access(browser)
    browser.result = BridgeReply(429, '{"error":{"message":"busy"}}', {'retry-after': header})
    assert iao.request('POST', BASE + 'search', {}).status_code == 429
    browser.connection_id += 1
    browser.result = BridgeReply(200, '{}')
    with pytest.raises(yt.YouTubeMusicUnavailable) as caught:
        iao.request('POST', BASE + 'browse/edit_playlist', {}, identity='another')
    assert isinstance(caught.value, BridgeError)
    assert caught.value.retry_at == deadline
    assert caught.value.outcome == 'not-dispatched' and caught.value.mutation
    assert len(browser.calls) == 1
    clock[0] = deadline + 1
    assert iao.request('POST', BASE + 'browse', {}).status_code == 200
    assert iao.availability().ready(clock[0])


@pytest.mark.parametrize('failure', [
    BridgeError('secret URL or cookie'),
    BridgeReply(0, '{"error":"open music.youtube.com","outcome":"not-dispatched"}'),
    BridgeReply(0, '{"error":"response lost","outcome":"uncertain"}'),
    BridgeReply(0, 'invalid JSON'), BridgeReply(200, None),
])
def test_failed_mutation_is_never_replayed_or_reported_as_signed_out(failure):
    browser = Browser()
    iao, _ = access(browser)
    browser.result = failure
    with pytest.raises(yt.YouTubeMusicUnavailable) as caught:
        iao.request('POST', BASE + 'playlist/delete', {'playlistId': 'user-owned'})
    assert caught.value.mutation
    assert caught.value.outcome == ('not-dispatched' if isinstance(failure, BridgeReply)
        and failure[1] and 'not-dispatched' in failure[1] else 'uncertain')
    assert 'secret' not in str(caught.value) and '401' not in str(caught.value)
    assert len(browser.calls) == 1
    browser.result = BridgeReply(200, '{}')
    if caught.value.outcome == 'uncertain':
        assert iao.request('POST', BASE + 'browse', {}).status_code == 200
    else:
        with pytest.raises(yt.YouTubeMusicUnavailable):
            iao.request('POST', BASE + 'browse', {})
    with pytest.raises(yt.YouTubeMusicUnavailable) as blocked:
        iao.request('POST', BASE + 'playlist/delete', {})
    assert blocked.value.outcome == 'not-dispatched'
    browser.connection_id += 1
    assert iao.request('POST', BASE + 'browse', {}).status_code == 200


def test_domain_can_reconcile_an_ambiguous_write_with_a_safe_readback():
    from yt_playlist.library.playlist_mutation import confirm_playlist_write
    browser = Browser()
    iao, _ = access(browser)
    calls = []
    def execute(method, url, body):
        calls.append(url)
        if url.endswith('browse/edit_playlist'):
            return BridgeReply(0, '{"error":"response lost","outcome":"uncertain"}')
        return BridgeReply(200, '{"tracks":["confirmed"]}')
    browser.execute = execute
    confirm_playlist_write(
        lambda: iao.request('POST', BASE + 'browse/edit_playlist', {}).json(),
        lambda: iao.request('POST', BASE + 'browse', {}).json(),
        lambda result: result['tracks'] == ['confirmed'], 'reorder')
    assert calls == [BASE + 'browse/edit_playlist', BASE + 'browse']


def test_disconnected_admission_is_safe_and_cancellation_does_not_dispatch():
    browser = Browser()
    iao, _ = access(browser)
    browser.connected = False
    with pytest.raises(yt.YouTubeMusicUnavailable) as caught:
        iao.request('POST', BASE + 'playlist/create', {})
    assert caught.value.outcome == 'not-dispatched'
    browser.connected = True
    with pytest.raises(WorkClosing), cancellable_work(lambda: True):
        iao.request('POST', BASE + 'browse', {})
    assert not browser.calls and iao._in_flight == 0


def test_pacing_is_shared_without_holding_state_lock():
    browser = Browser()
    ticks = [10.0]
    sleeps = []
    iao = None
    def sleep(delay):
        # Another thread can inspect admission during a wait.
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(iao.availability).result(timeout=1).ready(1000)
        sleeps.append(delay)
        ticks[0] += delay
    iao = yt.YouTubeMusicIAO(browser, now_fn=lambda: 1000, monotonic_fn=lambda: ticks[0], sleep_fn=sleep)
    iao.request('POST', BASE + 'browse', {})
    iao.request('POST', BASE + 'next', {})
    assert sum(sleeps) >= yt.MIN_INTERVAL - .00001


def test_in_flight_success_cannot_erase_newer_outage_and_foreground_database_progresses(store):
    browser = Browser()
    iao, _ = access(browser)
    started, release = threading.Event(), threading.Event()
    def execute(method, url, body):
        if url.endswith('browse'):
            started.set()
            assert release.wait(2)
            return BridgeReply(200, '{}')
        return BridgeReply(503, '{"error":{"message":"unavailable"}}')
    browser.execute = execute
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(iao.request, 'POST', BASE + 'browse', {})
        try:
            assert started.wait(1)
            store.settings.set_setting('youtube-iao-foreground', 'working')
            assert store.settings.get_setting('youtube-iao-foreground') == 'working'
            assert iao.request('POST', BASE + 'next', {}).status_code == 503
        finally:
            release.set()
        assert pending.result(timeout=1).status_code == 200
    with pytest.raises(yt.YouTubeMusicUnavailable):
        iao.request('POST', BASE + 'player', {})
    assert iao._in_flight == 0


def test_concurrent_request_limit_has_bounded_admission(monkeypatch):
    browser = Browser()
    iao, _ = access(browser)
    started = threading.Barrier(yt.MAX_IN_FLIGHT + 1)
    release = threading.Event()
    def execute(*args):
        started.wait(timeout=2)
        assert release.wait(2)
        return BridgeReply(200, '{}')
    browser.execute = execute
    with ThreadPoolExecutor(max_workers=yt.MAX_IN_FLIGHT) as pool:
        pending = [pool.submit(iao.request, 'POST', BASE + 'browse', {}) for _ in range(yt.MAX_IN_FLIGHT)]
        try:
            started.wait(timeout=2)
            monkeypatch.setattr(yt, 'ADMISSION_TIMEOUT', 0)
            with pytest.raises(yt.YouTubeMusicUnavailable) as caught:
                iao.request('POST', BASE + 'browse/edit_playlist', {})
            assert caught.value.outcome == 'not-dispatched'
        finally:
            release.set()
        assert all(future.result(timeout=1).status_code == 200 for future in pending)


def test_only_safe_retry_metadata_is_forwarded_by_bridge_reply():
    reply = BridgeReply(429, '{}', {'Retry-After': '120', 'Authorization': 'secret', 'Set-Cookie': 'secret'})
    assert reply == (429, '{}') and reply.headers == {'Retry-After': '120'}
    assert BridgeReply(429, '{}', {'retry-after': '1\r\nSet-Cookie: secret'}).headers == {}


def test_storyboard_transport_is_bounded_guarded_and_independent_of_session():
    url = 'https://i.ytimg.com/sb/abcdefghijk/storyboard3_L1/M0.jpg?sigh=signed'
    clock, calls = [1000.0], []
    class Response:
        status_code = 429
        headers = {'Retry-After': '120', 'Content-Type': 'image/jpeg'}
        closed = False
        def __enter__(self): return self
        def __exit__(self, *_): self.closed = True
        def iter_content(self, _): yield b'image'
    response = Response()
    def get(request_url, **options):
        calls.append((request_url, options))
        return response
    iao = yt.YouTubeStoryboardIAO(now_fn=lambda: clock[0], get_fn=get)
    deadline = time.monotonic() + 10
    with pytest.raises(yt.IntegrationUnavailable) as caught:
        iao.read(url, deadline)
    assert caught.value.retry_at == 1120 and response.closed
    with pytest.raises(yt.IntegrationUnavailable):
        iao.read(url, deadline)
    assert len(calls) == 1
    browser = Browser()
    api, _ = access(browser)
    assert api.request('POST', BASE + 'playlist/create', {}).status_code == 200
    clock[0], response.status_code = 1121, 200
    assert iao.read(url, deadline) == b'image'
    assert calls[-1][1]['allow_redirects'] is False and calls[-1][1]['stream'] is True
    with pytest.raises(ValueError):
        iao.read('https://evil.example/sb/abcdefghijk/storyboard3_L1/M0.jpg', deadline)


def test_storyboard_timeout_and_cancel_keep_their_actual_outcomes():
    url = 'https://i.ytimg.com/sb/abcdefghijk/storyboard3_L1/M0.jpg?sigh=signed'
    calls = []
    def get(*args, **kwargs):
        calls.append(True)
        raise requests.Timeout('private URL')
    iao = yt.YouTubeStoryboardIAO(get_fn=get)
    with pytest.raises(yt.IntegrationUnavailable) as caught:
        iao.read(url, time.monotonic() + 10)
    assert 'private' not in str(caught.value)
    with pytest.raises(WorkClosing), cancellable_work(lambda: True):
        iao.read(url, time.monotonic() + 10)
    assert len(calls) == 1


@pytest.mark.parametrize('older,newer,advance,expected,reason', [
    ('timeout', 600, 0, 1600, 'service'),
    (60, 600, 0, 1600, 'service'),
    (600, 60, 0, 1600, 'service'),
    ('success', 600, 0, 1600, 'service'),
    ('timeout', 600, 595, 1600, 'service'),
    ('timeout', 600, 601, 1606, 'transport'),
])
def test_overlapping_storyboards_preserve_longest_retry_and_its_reason(
        older, newer, advance, expected, reason):
    url = 'https://i.ytimg.com/sb/abcdefghijk/storyboard3_L1/M0.jpg?sigh=controlled'
    started, release = threading.Event(), threading.Event()
    clock, calls = [1000.0], []

    class Response:
        def __init__(self, retry=None):
            self.status_code = 200 if retry is None else 429
            self.headers = {'Content-Type': 'image/jpeg'}
            if retry is not None:
                self.headers['Retry-After'] = str(retry)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def iter_content(self, _):
            yield b'image'

    def get(request_url, **kwargs):
        calls.append(request_url)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
            if older == 'timeout':
                raise requests.Timeout('controlled older request')
            return Response(None if older == 'success' else older)
        return Response(newer if len(calls) == 2 else None)

    iao = yt.YouTubeStoryboardIAO(now_fn=lambda: clock[0], get_fn=get)
    deadline = time.monotonic() + 30
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(iao.read, url, deadline)
        try:
            assert started.wait(2)
            with pytest.raises(yt.IntegrationUnavailable) as recent:
                iao.read(url, deadline)
            assert recent.value.retry_at == 1000 + max(30, newer)
            clock[0] += advance
        finally:
            release.set()
        if older == 'success':
            assert pending.result(timeout=2) == b'image'
        else:
            with pytest.raises(yt.IntegrationUnavailable) as previous:
                pending.result(timeout=2)
            assert previous.value.retry_at == expected
            assert reason in str(previous.value)

    clock[0] = expected - 1
    with pytest.raises(yt.IntegrationUnavailable) as retained:
        iao.read(url, deadline)
    assert retained.value.retry_at == expected
    assert reason in str(retained.value)
    assert len(calls) == 2
    clock[0] = expected + 1
    assert iao.read(url, deadline) == b'image'
    assert len(calls) == 3
