"""Optional artwork failures recover after a completed metadata pass, including after restart."""
from collections import OrderedDict
from functools import partial
import logging
from types import SimpleNamespace
import urllib.error

import pytest

from yt_playlist.core.store import Store
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.providers import coverart
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall


class Provider:
    name = "musicbrainz"
    def __init__(self): self.calls = []
    def available(self, store): return True
    def reset(self): pass
    def tripped(self): return False
    def probe(self, track, store):
        self.calls.append(track["id"])
        return EnrichmentResult(self.name, {"genre":"techno", "year":"2024", "bpm":120,
            "energy":.8, "danceability":.7, "mb_release_ids":["release"]})


def worker(store, clock, provider):
    ctx = SimpleNamespace(store=store, now_fn=lambda: clock[0], logger=logging.getLogger(__name__),
                          rec_worker=SimpleNamespace(trigger_mode_refresh=lambda **kw: None))
    return EnrichWorker(ctx, waterfall_fn=partial(run_waterfall, registry={provider.name:provider}))


def test_transient_art_failure_recovers_after_restart_without_repeating_complete_metadata(tmp_path, monkeypatch):
    clock, mono = [1000.0], [100.0]
    monkeypatch.setattr(coverart.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(coverart, "_cache", OrderedDict())
    calls, recovered = [], [False]
    def fetch(url):
        calls.append(url)
        if not recovered[0]:
            raise TimeoutError("archive offline")
        return {"images":[{"front":True,"image":"https://cover.example/real.jpg"}]}
    monkeypatch.setattr(coverart, "_get_json", fetch)
    path = tmp_path / "art.db"
    store = Store(path); store.init_schema()
    tid = store.upsert_track("v", "Song", "Artist", None, 200)
    provider = Provider()
    w = worker(store, clock, provider)
    assert w.drain_once() == 1
    assert store.coverage_stats()["processed"] == 1
    assert store.queue_remaining() == 0
    assert store.enrichment.artwork_retry_status() == {"count":1,"retry_at":1060}
    assert len(calls) == 1
    store.conn.close()
    # Simulate a new process: persistent schedule survives, while the response cache does not.
    coverart._cache.clear()
    store = Store(path); store.init_schema()
    w = worker(store, clock, provider)
    clock[0], mono[0] = 1059, 159
    assert w.drain_once() == 0
    assert len(calls) == 1
    recovered[0] = True
    clock[0], mono[0] = 1060, 160
    assert w.drain_once() == 1
    assert store.catalog_subject(tid)["thumbnail"] == "https://cover.example/real.jpg"
    assert provider.calls == [tid]
    assert store.enrichment.artwork_retry_status()["count"] == 0
    assert store.conn.execute("SELECT status FROM enrichment_artwork").fetchone()[0] == "found"
    store.conn.close()


def test_cached_failures_retain_retry_outcome_and_due_job_waits_for_cache_expiry(store, monkeypatch):
    clock = [1000.0]
    mono = [100.0]
    monkeypatch.setattr(coverart.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(coverart, "_cache", OrderedDict())
    calls = []
    def fetch(url):
        calls.append(url)
        raise urllib.error.HTTPError(url, 503, "busy", {"Retry-After":"120"}, None)
    monkeypatch.setattr(coverart, "_get_json", fetch)
    provider = Provider()
    store.upsert_track("v1", "One", "Artist", None, 200)
    w = worker(store, clock, provider)
    assert w.drain_once() == 1
    assert store.enrichment.artwork_retry_status()["retry_at"] >= 1120
    clock[0], mono[0] = 1010, 110
    store.upsert_track("v2", "Two", "Artist", None, 200)
    assert w.drain_once() == 1
    assert len(calls) == 1                        # second track sees cached failure, not absence
    assert store.enrichment.artwork_retry_status()["count"] == 2
    assert store.coverage_stats()["processed"] == 2
    assert store.queue_remaining() == 0


@pytest.mark.parametrize("status", [400,404])
def test_confirmed_absence_has_no_retry_job(store, monkeypatch, status):
    monkeypatch.setattr(coverart, "_cache", OrderedDict())
    def missing(url):
        raise urllib.error.HTTPError(url, status, "no cover", {}, None)
    monkeypatch.setattr(coverart, "_get_json", missing)
    tid = store.upsert_track("v", "Song", "Artist", None, 200)
    assert worker(store, [1000], Provider()).drain_once() == 1
    assert store.conn.execute("SELECT status FROM enrichment_artwork WHERE entry_id=?", (tid,)).fetchone()[0] == "absent"
    assert store.enrichment.artwork_retry_status()["count"] == 0


def test_retry_preserves_useful_cover_added_after_failure(store, monkeypatch):
    from yt_playlist.enrich.artwork import retry_due
    tid = store.upsert_track("v", "Song", "Artist", None, 200)
    store.enrichment.record_artwork_result(tid, ["release"], coverart.CoverResult("retry", retry_in=60), 1000)
    store.set_track_art(tid, "https://cover.example/user.jpg")
    def unexpected(*args):
        raise AssertionError("useful cover must prevent another archive call")
    monkeypatch.setattr(coverart, "front_cover", unexpected)
    assert retry_due(store, lambda:1060) == 1
    assert store.catalog_subject(tid)["thumbnail"] == "https://cover.example/user.jpg"
    assert store.enrichment.artwork_retry_status()["count"] == 0
