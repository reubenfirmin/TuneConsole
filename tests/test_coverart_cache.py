"""Tracks sharing a release must share artwork results, including transient failures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import io
import json
import logging
import threading
from types import SimpleNamespace
import urllib.error

import pytest

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations import coverartarchive as access
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.providers import coverart
from yt_playlist.util import net


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(coverart, '_iao', access.CoverArtArchiveIAO(
        now_fn=lambda: now[0], monotonic_fn=lambda: now[0],
        pacer=SimpleNamespace(wait=lambda: None)))
    return now


def transport(monkeypatch, fetch):
    def open_request(request, *, timeout):
        assert timeout == access.TIMEOUT_S == 20
        assert request.get_header('User-agent') == access.USER_AGENT
        assert request.get_header('Accept') == 'application/json'
        data = fetch(request.full_url)
        return io.BytesIO(data if isinstance(data, bytes) else json.dumps(data).encode())
    monkeypatch.setattr(coverart._iao, 'open', open_request)


def artwork(url="https://art.example/cover.jpg"):
    return {"images": [{"front": True, "thumbnails": {"500": url}}]}


def test_repeated_500s_are_delayed_and_logged_once_per_attempt(clock, monkeypatch, caplog):
    calls = []

    def unavailable(url):
        calls.append(url)
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    transport(monkeypatch, unavailable)
    caplog.set_level(logging.INFO, logger=access.__name__)
    assert coverart.front_cover(["release"]) is None
    clock[0] += 19
    assert coverart.front_cover(["release"]) is None
    clock[0] += 9
    assert coverart.front_cover(["release"]) is None
    assert len(calls) == 1
    assert len(caplog.records) == 1
    assert 'retry_at=160.000' in caplog.text
    # A new enrichment batch must not erase the cooldown.
    coverart.reset()
    assert coverart.front_cover(["release"]) is None
    assert len(calls) == 1
    clock[0] = 160
    assert coverart.front_cover(["release"]) is None
    assert len(calls) == 2
    assert 'retry_at=280.000' in caplog.text
    clock[0] = 279
    assert coverart.front_cover(["release"]) is None
    assert len(calls) == 2


def test_failure_backoff_is_capped_and_recovers(clock, monkeypatch):
    calls = []
    recovering = [False]

    def fetch(url):
        calls.append(url)
        if recovering[0]:
            return artwork()
        raise TimeoutError("slow archive")

    transport(monkeypatch, fetch)
    for delay in (60, 120, 240, 480, 900, 900):
        assert coverart.front_cover(["release"]) is None
        assert coverart._iao.availability().retry_at == clock[0] + delay
        clock[0] += delay
    recovering[0] = True
    assert coverart.front_cover(["release"]) == "https://art.example/cover.jpg"
    assert coverart._iao.availability().ready(clock[0])
    assert coverart.front_cover(["release"]) == "https://art.example/cover.jpg"
    assert len(calls) == 7


def test_missing_art_is_cached_and_other_editions_still_work(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/missing"):
            raise urllib.error.HTTPError(url, 404, "No cover", {}, None)
        return artwork()

    transport(monkeypatch, fetch)
    assert coverart.front_cover(["missing", "available"]) == "https://art.example/cover.jpg"
    assert not coverart.tripped()
    assert coverart.front_cover(["missing", "available"]) == "https://art.example/cover.jpg"
    assert len(calls) == 2
    clock[0] += access.MISS_TTL
    assert coverart.front_cover(["missing", "available"]) == "https://art.example/cover.jpg"
    assert len(calls) == 3


def test_server_error_defers_uncached_releases_but_keeps_a_cached_cover(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/broken"):
            raise urllib.error.HTTPError(url, 500, "Error", {}, None)
        return artwork()

    transport(monkeypatch, fetch)
    assert coverart.lookup(['cached']).status == 'found'
    for _ in range(3):
        assert coverart.lookup(['broken', 'working']).status == 'retry'
        assert coverart.lookup(['broken', 'cached']).status == 'found'
    assert len(calls) == 2
    clock[0] += access.ERROR_RETRY
    assert coverart.lookup(['working']).status == 'found'
    assert len(calls) == 3 and not coverart.tripped()


def test_empty_cover_response_is_cached_but_rechecked_later(clock, monkeypatch):
    calls = []
    transport(monkeypatch, lambda url: calls.append(url) or {'images': []})
    assert coverart.front_cover(["empty"]) is None
    assert coverart.front_cover(["empty"]) is None
    assert len(calls) == 1
    clock[0] += access.MISS_TTL
    assert coverart.front_cover(["empty"]) is None
    assert len(calls) == 2


def test_cache_is_bounded_and_retains_recently_used_releases(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)
    transport(monkeypatch, lambda url: calls.append(url) or artwork())
    for release in ("a", "b", "a", "c", "a"):
        assert coverart.front_cover([release])
    assert len(calls) == 3
    assert set(coverart._iao._cache) == {'a', 'c'}
    assert coverart.front_cover(["b"])
    assert len(calls) == 4
    assert len(coverart._iao._cache) == 2


def test_positive_cache_expires_and_duplicate_candidates_do_not_use_the_limit(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return artwork("http://art.example/cover.jpg") if url.endswith("/found") else {"images": []}

    transport(monkeypatch, fetch)
    assert coverart.front_cover(["empty"] * 4 + ["found"]) == "https://art.example/cover.jpg"
    assert len(calls) == 2
    clock[0] += access.COVER_TTL
    assert coverart.front_cover(["found"]) == "https://art.example/cover.jpg"
    assert len(calls) == 3


def test_ttl_starts_after_a_slow_failed_request(clock, monkeypatch):
    def fetch(url):
        clock[0] += 20
        raise ValueError("malformed JSON")

    transport(monkeypatch, fetch)
    assert coverart.front_cover(["release"]) is None
    assert coverart._iao._waits['release'][0].retry_at == 180


def test_overlapping_jobs_share_one_request_without_holding_database_lock(clock, monkeypatch, store):
    started, finish = threading.Event(), threading.Event()
    ready = threading.Barrier(3)
    calls = []

    def fetch(url):
        assert not store._lock.held_by_current_thread
        calls.append(url)
        started.set()
        assert finish.wait(timeout=2)
        return artwork()

    def lookup():
        ready.wait(timeout=2)
        with coverart.access(store):
            return coverart.front_cover(['shared'])

    transport(monkeypatch, fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = [pool.submit(lookup) for _ in range(2)]
        ready.wait(timeout=2)
        assert started.wait(timeout=2)
        try:
            pool.submit(store.settings.set_setting, 'unrelated', 'updated').result(timeout=2)
        finally:
            finish.set()
        assert [r.result(timeout=2) for r in results] == ["https://art.example/cover.jpg"] * 2
    assert len(calls) == 1


@pytest.mark.parametrize('failure', [TimeoutError('offline'),
    urllib.error.HTTPError(access.API, 503, 'busy', {}, None),
    urllib.error.HTTPError(access.API, 429, 'quota', {}, None)])
def test_host_failure_is_shared_between_release_ids_and_recovers(clock, monkeypatch, store, failure):
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise failure
        return artwork()
    transport(monkeypatch, fetch)
    with coverart.access(store):
        first = coverart.lookup(['one'])
        second = coverart.lookup(['two'])
    assert first.status == second.status == 'retry'
    assert first.retry_in == second.retry_in == 60
    assert len(calls) == 1
    assert store.enrichment_state.integration_cooldown('coverartarchive')['retry_at'] == 160
    clock[0] = 160
    with coverart.access(store):
        assert coverart.lookup(['two']).status == 'found'
    assert len(calls) == 2
    assert store.enrichment_state.integration_cooldown('coverartarchive') is None


@pytest.mark.parametrize('header,deadline', [
    ('120', 220),
    (format_datetime(datetime.fromtimestamp(600, tz=timezone.utc), usegmt=True), 600),
    ('bad', 160), ('inf', 160), ('-5', 160),
])
def test_retry_after_survives_recreation(clock, monkeypatch, store, header, deadline):
    def fetch(url):
        raise urllib.error.HTTPError(url, 429, 'busy', {'Retry-After': header}, None)
    transport(monkeypatch, fetch)
    with coverart.access(store):
        assert coverart.lookup(['one']).retry_in == deadline - clock[0]
    recreated = access.CoverArtArchiveIAO(now_fn=lambda: clock[0],
        pacer=SimpleNamespace(wait=lambda: pytest.fail('Deferred requests must not pace')),
        open_fn=lambda *a, **kw: pytest.fail('Persisted cooldown was bypassed'))
    with recreated.context(store), pytest.raises(IntegrationUnavailable) as error:
        recreated.release('different')
    assert error.value.provider == 'coverartarchive' and error.value.retry_at == deadline


@pytest.mark.parametrize('bad', [b'invalid JSON', [], {}, {'error': 'busy', 'images': []},
    {'images': {}}, {'images': [None]}, {'images': [{'front': True}]},
    {'images': [{'front': True, 'thumbnails': []}]},
    {'images': [{'front': True, 'image': 123}]},
    {'images': [{'front': True, 'thumbnails': {'small': 'https://art.example/x'}}]}])
def test_malformed_response_is_pending_for_its_release(clock, monkeypatch, bad):
    calls = []

    def fetch(url):
        calls.append(url)
        return bad if url.endswith('/bad') else artwork()
    transport(monkeypatch, fetch)
    first = coverart.lookup(['bad'])
    assert first.status == 'retry' and not coverart.tripped()
    assert coverart.lookup(['bad']).status == 'retry'
    assert coverart.lookup(['good']).status == 'found'
    assert len(calls) == 2 and 'bad' not in coverart._iao._cache


def test_bad_request_is_pending_and_does_not_block_other_editions(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith('/bad'):
            raise urllib.error.HTTPError(url, 400, 'invalid request', {}, None)
        return artwork()
    transport(monkeypatch, fetch)
    assert coverart.lookup(['bad']).status == 'retry'
    assert coverart.lookup(['bad', 'good']).status == 'found'
    assert not coverart.tripped() and len(calls) == 2


def test_stale_cover_is_retained_but_refresh_remains_pending_until_recovery(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 2:
            raise TimeoutError('offline')
        return artwork()
    transport(monkeypatch, fetch)
    assert coverart.lookup(['release']).status == 'found'
    clock[0] += access.COVER_TTL
    first = coverart.lookup(['release'])
    second = coverart.lookup(['release'])
    assert first.status == second.status == 'retry'
    assert first.url == second.url == 'https://art.example/cover.jpg'
    assert len(calls) == 2
    clock[0] += 60
    assert coverart.lookup(['release']).status == 'found'
    assert len(calls) == 3


def test_raw_cache_is_detached_and_success_ttl_starts_after_response(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        clock[0] += 20
        return artwork()
    transport(monkeypatch, fetch)
    result = coverart._iao.release('release')
    result['images'].clear()
    clock[0] = 120 + access.COVER_TTL - 1
    assert coverart.lookup(['release']).status == 'found'
    assert len(calls) == 1
    clock[0] += 1
    assert coverart.lookup(['release']).status == 'found'
    assert len(calls) == 2


def test_success_cannot_clear_a_newer_persisted_cooldown(clock, monkeypatch, store):
    def fetch(url):
        store.enrichment_state.defer_integration('coverartarchive', now=clock[0],
            retry_after=500, error='another process is throttled')
        return artwork()
    transport(monkeypatch, fetch)
    with coverart.access(store):
        assert coverart.lookup(['release']).status == 'found'
        assert coverart.tripped()
    assert store.enrichment_state.integration_cooldown('coverartarchive')['retry_at'] == 500


@pytest.mark.parametrize('failure', [WorkClosing(), WorkBudgetExceeded()])
def test_cancellation_propagates_without_retry_or_miss(clock, monkeypatch, failure):
    def fetch(url):
        raise failure
    transport(monkeypatch, fetch)
    with net.probe_errors() as errors, pytest.raises(type(failure)):
        coverart.lookup(['release'])
    assert not errors and not coverart._iao._cache and not coverart._iao._waits


@pytest.mark.parametrize('identity', ['../release', 'id?extra=1', 'id#fragment',
    'https://evil.example', '', None])
def test_boundary_rejects_unexpected_resource_paths(clock, monkeypatch, identity):
    transport(monkeypatch, lambda url: pytest.fail('Invalid resource reached transport'))
    with pytest.raises(ValueError):
        coverart._iao.release(identity)


def test_request_waits_are_bounded_and_do_not_affect_other_releases(clock, monkeypatch):
    monkeypatch.setattr(access, 'CACHE_SIZE', 2)

    def fetch(url):
        raise ValueError('malformed response')
    transport(monkeypatch, fetch)
    for identity in ('a', 'b', 'c'):
        assert coverart.lookup([identity]).status == 'retry'
    assert list(coverart._iao._waits) == ['b', 'c']
    assert not coverart.tripped()
