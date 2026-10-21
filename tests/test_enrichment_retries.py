"""Retry deadlines survive batches/restarts without starving other work or repeating successes."""
import logging
import urllib.error
from functools import partial
from types import SimpleNamespace

import pytest

from yt_playlist.core.store import Store
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.util import net


class Provider:
    def __init__(self, name, fail=None):
        self.name, self.fail, self.calls = name, fail, []

    def available(self, store): return True
    def reset(self): pass
    def tripped(self): return False

    def probe(self, track, store):
        self.calls.append(track["title"])
        if self.fail:
            self.fail(track)
        return EnrichmentResult(self.name, {"genre": "techno"})


def worker(store, clock, providers):
    ctx = SimpleNamespace(store=store, now_fn=lambda: clock[0], logger=logging.getLogger(__name__),
                          rec_worker=SimpleNamespace(trigger_mode_refresh=lambda **kw: None))
    return EnrichWorker(ctx, waterfall_fn=partial(run_waterfall,
                        registry={p.name: p for p in providers}))


def tracks(store, count):
    store.set_setting("enrich_caught_up_at", "100")
    return [store.upsert_track(f"v{i}", f"Song {i}", "Artist", None, 200,
                               created_at=10-i) for i in range(count)]


def test_more_failures_than_one_batch_do_not_starve_healthy_tracks_and_recovery_reuses_success(tmp_path):
    path = tmp_path / "retries.db"
    store = Store(path); store.init_schema()
    ids = tracks(store, 7)
    clock = [1000.0]
    def fail(track):
        if track["title"] in {f"Song {i}" for i in range(5)}:
            raise ValueError("malformed response")
    broken, healthy = Provider("musicbrainz", fail), Provider("deezer")
    w = worker(store, clock, [broken, healthy])
    for _ in range(4):
        w.drain_once(limit=2)
        clock[0] += 31
    assert {t["id"] for t in store.enriched_tracks()} == set(ids[5:])
    assert healthy.calls == [f"Song {i}" for i in range(7)]
    assert store.queue_remaining() == 5
    assert "malformed response" in w.activity["warning"]
    key = store.catalog_subject(ids[0])["identity_key"]
    assert store.enrichment_attempts(key)["deezer"]["fields"] == {"genre": "techno"}
    # A new process uses saved deadlines and successful provider evidence.
    store.conn.close()
    store = Store(path); store.init_schema()
    broken.fail = None
    w = worker(store, clock, [broken, healthy])
    clock[0] += 3600
    for _ in range(3):
        w.drain_once(limit=2)
    assert store.queue_remaining() == 0
    assert len(healthy.calls) == 7
    assert len(store.enriched_tracks()) == 7
    store.conn.close()


@pytest.mark.parametrize("retry_after", ["120", "Thu, 01 Jan 1970 00:18:40 GMT"])
def test_throttle_cooldown_honors_retry_after_across_batches(store, retry_after):
    tracks(store, 4)
    clock = [1000.0]
    def throttle(track):
        raise urllib.error.HTTPError("https://provider.test", 429, "slow down",
                                     {"Retry-After": retry_after}, None)
    throttled, healthy = Provider("musicbrainz", throttle), Provider("deezer")
    w = worker(store, clock, [throttled, healthy])
    assert w.drain_once(limit=4) == 0
    assert len(throttled.calls) == 1 and len(healthy.calls) == 4
    assert store.enrichment_cooldowns()["musicbrainz"]["retry_at"] == 1120
    clock[0] = 1119
    assert w.drain_once(limit=4) == 0
    assert len(throttled.calls) == 1
    assert store.next_enrich_batch(4, providers=["musicbrainz", "deezer"], now=1119) == []
    assert store.queue_remaining() == 4
    clock[0] = 1120
    throttled.fail = None
    assert w.drain_once(limit=4) == 4
    assert len(healthy.calls) == 4
    assert store.enrichment_cooldowns() == {}


def test_backoff_is_bounded_and_partial_provider_evidence_is_retained(store):
    tid = tracks(store, 1)[0]
    track = store.catalog_subject(tid)
    for i in range(12):
        store.record_enrichment_attempt(track["identity_key"], "musicbrainz",
            {"mb_recording_id": "mbid"} if i == 0 else {}, retry=True, now=10000*i,
            video_id=track["video_id"], error="invalid JSON")
        state = store.enrichment_provider_state(track["recording_id"])["musicbrainz"]
        assert 30 <= state["retry_at"] - 10000*i <= 3600
    assert state["failures"] == 12
    assert state["fields"] == {"mb_recording_id": "mbid"}
    # A completed pass starts a new cycle on the next stale sweep.
    store.mark_recordings_enriched([track["recording_id"]], 200000)
    assert store.enrichment_provider_state(track["recording_id"]) == {}


def test_retry_after_invalid_header_uses_default_backoff():
    error = urllib.error.HTTPError("u", 503, "busy", {"Retry-After": "nonsense"}, None)
    assert net.retry_after(error, 1000) == 0
