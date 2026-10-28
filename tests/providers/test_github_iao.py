"""Public update-check access sharing and recovery through the real scheduler."""
import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from yt_playlist.core import updatecheck
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.integrations import github
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.util import net


@pytest.fixture
def boundary(store, monkeypatch):
    def install(fetch=lambda request: {'tag_name': 'v0.2.0'}):
        clock, calls, responses, events = [1000.0], [], [], []

        def pace():
            assert not store._lock.held_by_current_thread and not store.conn.in_transaction
            events.append('pace')

        def open_request(request, *, timeout):
            assert not store._lock.held_by_current_thread and not store.conn.in_transaction
            calls.append((request, timeout))
            events.append('request')
            body = fetch(request)
            headers = {}
            if isinstance(body, tuple):
                body, headers = body
            response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
            response.headers = headers
            responses.append(response)
            return response

        iao = github.GitHubReleasesIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
            open_fn=open_request, pacer=SimpleNamespace(wait=pace))
        monkeypatch.setattr(updatecheck, '_github', iao)
        return SimpleNamespace(iao=iao, clock=clock, calls=calls, responses=responses, events=events)
    return install


def test_scheduler_uses_exact_public_transport_and_detached_shared_metadata(boundary, store, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'SECRET')
    monkeypatch.setenv('GH_TOKEN', 'ALSO_SECRET')
    b = boundary()
    updatecheck.check_latest(store, 1000)
    first = b.iao.latest_release()
    first['tag_name'] = 'mutated'
    assert updatecheck._fetch_latest_release() == '0.2.0'
    assert store.settings.get_setting('latest_version_seen') == '0.2.0'
    assert len(b.calls) == 1 and b.events == ['pace', 'request']
    request, timeout = b.calls[0]
    assert request.full_url == github.RELEASES_URL
    assert request.get_method() == 'GET' and request.data is None
    assert request.get_header('User-agent') == 'yt-playlist-updatecheck'
    assert request.get_header('Accept') == 'application/vnd.github+json'
    assert request.get_header('Authorization') is None and timeout == 8
    assert b.responses[0].closed and github.GitHubReleasesIAO().pacer.min_interval == 1


def test_overlapping_scheduler_checks_share_one_vendor_request(boundary, store):
    b = boundary()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: updatecheck.check_latest(store, 1000, interval_s=0), range(8)))
    assert len(b.calls) == 1
    assert store.settings.get_setting('latest_version_seen') == '0.2.0'
    assert store.settings.get_setting('latest_version_checked_at') == '1000'


@pytest.mark.parametrize('code,headers,deadline', [
    (429, {'Retry-After': '120'}, 1120),
    (503, {'Retry-After': '7200'}, 8200),
    (503, {'Retry-After': 'Thu, 01 Jan 1970 00:18:40 GMT'}, 1120),
    (403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1300'}, 1300),
    (403, {'Retry-After': '90'}, 1090),
    (403, {}, 1060), (503, {'Retry-After': 'NaN'}, 1060),
    (429, {'Retry-After': 'inf', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': 'inf'}, 1060),
])
def test_http_failure_deadlines_are_shared_and_persisted(boundary, store, code, headers, deadline, caplog):
    body = io.BytesIO(b'SECRET vendor response')

    def fetch(request):
        raise urllib.error.HTTPError(request.full_url, code, 'SECRET vendor message', headers, body)

    b = boundary(fetch)
    store.settings.set_setting('latest_version_seen', '0.1.0')
    with net.probe_errors() as errors:
        for _ in range(2):
            updatecheck.check_latest(store, 1000, interval_s=0)
    assert len(b.calls) == 1 and len(errors) == 2
    assert all(error.provider == 'github' and error.retry_at == deadline for error in errors)
    assert store.settings.get_setting('latest_version_seen') == '0.1.0'
    assert store.settings.get_setting('latest_version_checked_at') == '1000'
    saved = store.enrichment_state.integration_cooldown('github')
    assert saved['retry_at'] == deadline and saved['failures'] == 1
    assert 'SECRET' not in saved['error'] and 'SECRET' not in caplog.text and body.closed
    restarted = github.GitHubReleasesIAO(now_fn=lambda: 1000,
        open_fn=lambda *a, **kw: pytest.fail('Restart bypassed durable availability'),
        pacer=SimpleNamespace(wait=lambda: pytest.fail('Blocked access was paced')))
    with restarted.context(store.enrichment_state), pytest.raises(IntegrationUnavailable) as caught:
        restarted.latest_release()
    assert caught.value.retry_at == deadline


@pytest.mark.parametrize('failure', ['timeout', 'invalid_json', 'invalid_object'])
def test_expired_last_good_survives_temporary_failure_and_recovers(boundary, store, failure):
    failed = [False]

    def fetch(request):
        if failed[0]:
            if failure == 'timeout':
                raise TimeoutError('controlled timeout')
            return b'invalid json' if failure == 'invalid_json' else {'message': 'unavailable'}
        return {'tag_name': 'v0.2.0' if len(b.calls) == 1 else 'v0.3.0'}

    b = boundary(fetch)
    updatecheck.check_latest(store, 1000)
    b.clock[0] += github.CACHE_TTL
    failed[0] = True
    with net.probe_errors() as errors:
        updatecheck.check_latest(store, b.clock[0], interval_s=0)
        assert updatecheck._fetch_latest_release() == '0.2.0'
    assert len(b.calls) == 2 and len(errors) == 2
    assert store.settings.get_setting('latest_version_seen') == '0.2.0'
    b.clock[0] = errors[0].retry_at
    failed[0] = False
    with net.probe_errors() as recovered:
        updatecheck.check_latest(store, b.clock[0], interval_s=0)
    assert not recovered and len(b.calls) == 3
    assert store.settings.get_setting('latest_version_seen') == '0.3.0'
    assert store.enrichment_state.integration_cooldown('github') is None


@pytest.mark.parametrize('payload', [[], {}, {'tag_name': None}, {'tag_name': 2},
    {'tag_name': ''}, {'tag_name': ' '}, {'tag_name': 'v0.2.0', 'draft': True},
    {'tag_name': 'v0.2.0', 'prerelease': True}, {'tag_name': 'x' * 257},
    {'tag_name': 'v0.2.0', 'message': 'temporary error'}])
def test_invalid_successful_http_payloads_remain_retryable(boundary, payload):
    b = boundary(lambda request: payload)
    with pytest.raises(IntegrationUnavailable):
        b.iao.latest_release()
    assert b.iao._cached is None and b.responses[0].closed


def test_response_read_is_bounded_before_decode_or_cache(boundary):
    b = boundary(lambda request: b' ' * (github.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(IntegrationUnavailable):
        b.iao.latest_release()
    assert b.iao._cached is None and b.responses[0].closed


def test_no_release_is_a_short_confirmed_miss_and_preserves_seen_version(boundary, store):
    body = io.BytesIO(b'not found')

    def fetch(request):
        raise urllib.error.HTTPError(request.full_url, 404, 'not found', {}, body)

    b = boundary(fetch)
    store.settings.set_setting('latest_version_seen', '0.1.0')
    with net.probe_errors() as errors:
        updatecheck.check_latest(store, 1000)
        assert b.iao.latest_release() is None
    assert not errors and len(b.calls) == 1 and body.closed
    assert store.settings.get_setting('latest_version_seen') == '0.1.0'
    assert store.enrichment_state.integration_cooldown('github') is None


def test_primary_limit_headers_on_success_defer_uncached_work(boundary, store):
    b = boundary(lambda request: ({'tag_name': 'v0.2.0'},
        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1300'}))
    updatecheck.check_latest(store, 1000)
    assert store.enrichment_state.integration_cooldown('github')['retry_at'] == 1300
    b.clock[0] += github.CACHE_TTL
    with net.probe_errors() as errors:
        updatecheck.check_latest(store, b.clock[0], interval_s=0)
    assert len(b.calls) == 1 and errors[0].retry_at == 1300
    assert store.settings.get_setting('latest_version_seen') == '0.2.0'


def test_foreground_database_work_proceeds_while_update_check_waits_for_http(boundary, store):
    entered, release = Event(), Event()

    def fetch(request):
        entered.set()
        assert release.wait(5)
        return {'tag_name': 'v0.2.0'}

    boundary(fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(updatecheck.check_latest, store, 1000)
        try:
            assert entered.wait(5)
            pool.submit(store.settings.set_setting, 'foreground', 'saved').result(2)
            assert store.settings.get_setting('foreground') == 'saved'
        finally:
            release.set()
        update.result(5)


def test_reopened_database_keeps_cooldown_and_scoped_success_preserves_newer_wait(boundary, tmp_path):
    path = tmp_path / 'github.db'
    db = Store(path)
    db.init_schema()
    try:
        db.enrichment_state.defer_integration('github', now=1000, retry_after=1100, error='old')
    finally:
        db.conn.close()
    db = Store(path)
    db.init_schema()

    def fetch(request):
        db.enrichment_state.defer_integration('github', now=1101, retry_after=1300, error='new')
        return {'tag_name': 'v0.3.0'}

    b = boundary(fetch)
    try:
        updatecheck.check_latest(db, 1000, interval_s=0)
        assert not b.calls
        b.clock[0] = 1100
        updatecheck.check_latest(db, 1100, interval_s=0)
        assert db.settings.get_setting('latest_version_seen') == '0.3.0'
        assert db.enrichment_state.integration_cooldown('github')['retry_at'] == 1300
        assert b.iao.latest_release() == {'tag_name': 'v0.3.0'}
        assert db.enrichment_state.integration_cooldown('github')['retry_at'] == 1300
    finally:
        db.conn.close()


@pytest.mark.parametrize('error_type', [WorkClosing, WorkBudgetExceeded])
def test_cancellation_does_not_stamp_a_completed_check(boundary, store, error_type):
    def fetch(request):
        raise error_type('controlled cancellation')

    b = boundary(fetch)
    with net.probe_errors() as errors, pytest.raises(error_type):
        updatecheck.check_latest(store, 1000)
    assert not errors and b.iao._cached is None
    assert store.settings.get_setting('latest_version_checked_at') is None
    assert store.enrichment_state.integration_cooldown('github') is None


def test_scoped_repository_is_restored_before_a_standalone_call(boundary, store):
    b = boundary()
    store.enrichment_state.defer_integration('github', now=1000, retry_after=1300, error='saved')
    with b.iao.context(store.enrichment_state):
        with b.iao.context(), pytest.raises(IntegrationUnavailable):
            b.iao.latest_release()
    assert b.iao.latest_release() == {'tag_name': 'v0.2.0'}
    assert len(b.calls) == 1
    assert store.enrichment_state.integration_cooldown('github')['retry_at'] == 1300


def test_cache_retention_starts_after_slow_transport_finishes(boundary):
    def fetch(request):
        b.clock[0] += 8
        return {'tag_name': 'v0.2.0'}

    b = boundary(fetch)
    b.iao.latest_release()
    b.clock[0] += github.CACHE_TTL - 1
    b.iao.latest_release()
    assert len(b.calls) == 1
    b.clock[0] += 1
    b.iao.latest_release()
    assert len(b.calls) == 2
