"""Tracks sharing a release must share artwork results, including transient failures."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import urllib.error

import pytest

from yt_playlist.providers import coverart
from yt_playlist.util.net import CircuitBreaker


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(coverart.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(coverart, "_cache", OrderedDict())
    monkeypatch.setattr(coverart, "_breaker", CircuitBreaker())
    return now


def artwork(url="https://art.example/cover.jpg"):
    return {"images": [{"front": True, "thumbnails": {"500": url}}]}


def test_repeated_500s_are_delayed_and_logged_once_per_attempt(clock, monkeypatch, caplog):
    calls = []

    def unavailable(url):
        calls.append(url)
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(coverart, "_get_json", unavailable)
    caplog.set_level(logging.INFO, logger=coverart.__name__)
    assert coverart.front_cover(["release"]) is None
    clock[0] += 19
    assert coverart.front_cover(["release"]) is None
    clock[0] += 9
    assert coverart.front_cover(["release"]) is None
    assert len(calls) == 1
    assert len(caplog.records) == 1
    assert "retrying in 60s" in caplog.text
    # A new enrichment batch must not erase the cooldown.
    coverart.reset()
    assert coverart.front_cover(["release"]) is None
    assert len(calls) == 1
    clock[0] = 160
    assert coverart.front_cover(["release"]) is None
    assert len(calls) == 2
    assert "retrying in 120s" in caplog.text
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

    monkeypatch.setattr(coverart, "_get_json", fetch)
    for delay in (60, 120, 240, 480, 900, 900):
        assert coverart.front_cover(["release"]) is None
        assert coverart._cache["release"].expires_at == clock[0] + delay
        clock[0] += delay
    recovering[0] = True
    assert coverart.front_cover(["release"]) == "https://art.example/cover.jpg"
    assert coverart._cache["release"].failures == 0
    assert coverart.front_cover(["release"]) == "https://art.example/cover.jpg"
    assert len(calls) == 7


@pytest.mark.parametrize("status", [400, 404])
def test_missing_art_is_cached_and_other_editions_still_work(clock, monkeypatch, status):
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/missing"):
            raise urllib.error.HTTPError(url, status, "No cover", {}, None)
        return artwork()

    monkeypatch.setattr(coverart, "_get_json", fetch)
    coverart._breaker.record(urllib.error.URLError("previous network failure"))
    assert coverart.front_cover(["missing", "available"]) == "https://art.example/cover.jpg"
    assert not coverart._breaker.consecutive
    assert coverart.front_cover(["missing", "available"]) == "https://art.example/cover.jpg"
    assert len(calls) == 2
    clock[0] += coverart._MISS_TTL_S
    assert coverart.front_cover(["missing", "available"]) == "https://art.example/cover.jpg"
    assert len(calls) == 3


def test_server_error_delays_only_its_release(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/broken"):
            raise urllib.error.HTTPError(url, 500, "Error", {}, None)
        return artwork()

    monkeypatch.setattr(coverart, "_get_json", fetch)
    for _ in range(3):
        assert coverart.front_cover(["broken", "working"]) == "https://art.example/cover.jpg"
    assert len(calls) == 2


def test_empty_cover_response_is_cached_but_rechecked_later(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(coverart, "_get_json", lambda url: calls.append(url) or {"images": []})
    assert coverart.front_cover(["empty"]) is None
    assert coverart.front_cover(["empty"]) is None
    assert len(calls) == 1
    clock[0] += coverart._MISS_TTL_S
    assert coverart.front_cover(["empty"]) is None
    assert len(calls) == 2


def test_cache_is_bounded_and_retains_recently_used_releases(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(coverart, "_CACHE_SIZE", 2)
    monkeypatch.setattr(coverart, "_get_json", lambda url: calls.append(url) or artwork())
    for release in ("a", "b", "a", "c", "a"):
        assert coverart.front_cover([release])
    assert len(calls) == 3
    assert set(coverart._cache) == {"a", "c"}
    assert coverart.front_cover(["b"])
    assert len(calls) == 4
    assert len(coverart._cache) == 2


def test_positive_cache_expires_and_duplicate_candidates_do_not_use_the_limit(clock, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return artwork("http://art.example/cover.jpg") if url.endswith("/found") else {"images": []}

    monkeypatch.setattr(coverart, "_get_json", fetch)
    assert coverart.front_cover(["empty"] * 4 + ["found"]) == "https://art.example/cover.jpg"
    assert len(calls) == 2
    clock[0] += coverart._COVER_TTL_S
    assert coverart.front_cover(["found"]) == "https://art.example/cover.jpg"
    assert len(calls) == 3


def test_ttl_starts_after_a_slow_failed_request(clock, monkeypatch):
    def fetch(url):
        clock[0] += 20
        raise ValueError("malformed JSON")

    monkeypatch.setattr(coverart, "_get_json", fetch)
    assert coverart.front_cover(["release"]) is None
    assert coverart._cache["release"].expires_at == 180


def test_overlapping_jobs_share_one_request(clock, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    ready = threading.Barrier(3)
    calls = []

    def fetch(url):
        calls.append(url)
        started.set()
        assert finish.wait(timeout=2)
        return artwork()

    def lookup():
        ready.wait(timeout=2)
        return coverart.front_cover(["shared"])

    monkeypatch.setattr(coverart, "_get_json", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(lookup) for _ in range(2)]
        ready.wait(timeout=2)
        assert started.wait(timeout=2)
        finish.set()
        assert [r.result(timeout=2) for r in results] == ["https://art.example/cover.jpg"] * 2
    assert len(calls) == 1
