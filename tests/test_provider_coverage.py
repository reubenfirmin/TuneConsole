"""Newly available providers revisit completed recordings without repeating other providers."""
import logging
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.providers import enrichment, lastfm
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from tests.test_enrichment_config import _client


class Provider:
    def __init__(self, name, available=lambda store: True):
        self.name, self.available, self.calls = name, available, []

    def reset(self): pass
    def tripped(self): return False
    def probe(self, track, store):
        self.calls.append(track["id"])
        return EnrichmentResult(self.name, {"genre": "house", "year": "2024", "bpm": 120,
                                          "energy": .8, "danceability": .7})


def config(store, enabled):
    enrichment.save_config(store, [{"name": p["name"], "enabled": p["name"] in enabled}
                                    for p in enrichment.PROVIDERS])


def worker(store, providers):
    ctx = SimpleNamespace(store=store, now_fn=lambda: 1000,
        logger=logging.getLogger(__name__), rec_worker=SimpleNamespace(trigger_mode_refresh=Mock()))
    return EnrichWorker(ctx, waterfall_fn=partial(run_waterfall,
                        registry={p.name: p for p in providers}))


def test_enabling_provider_revisits_fully_populated_tracks_and_preserves_user_choices(store):
    tid = store.upsert_track("v1", "Song", "Artist", None, 200)
    a, b = Provider("musicbrainz"), Provider("deezer")
    config(store, {"musicbrainz"})
    w = worker(store, [a, b])
    assert w.drain_once() == 1
    recording = store.catalog_subject(tid)["recording_id"]
    assert store.provider_coverage(recording) == {"musicbrainz":"found", "deezer":"disabled"}
    store.set_track_genre(tid, "Selected genre")
    assert w.drain_once() == 0
    config(store, {"musicbrainz", "deezer"})
    store.reconcile_provider_configuration(enrichment.provider_configuration(store, {a.name:a, b.name:b}))
    assert store.queue_remaining() == 1
    assert store.coverage_stats()["processed"] == 0
    assert store.provider_coverage(recording)["deezer"] == "pending"
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == [tid]
    assert store.get_track_enrichment(tid) == ("Selected genre", "2024")
    assert store.queue_remaining() == 0
    assert store.coverage_stats()["processed"] == 1
    assert w.drain_once() == 0
    statuses = {r["provider"]:r["status"] for r in store.conn.execute("SELECT * FROM enrichment_provider_config")}
    assert statuses == {"musicbrainz":"available", "deezer":"available"}


def test_adding_or_changing_effective_key_schedules_only_that_provider(store, monkeypatch):
    tid = store.upsert_track("v1", "Song", "Artist", None, 200)
    monkeypatch.setattr(lastfm, "api_key", lambda s: s.get_setting("lastfm_api_key") or None)
    a = Provider("musicbrainz")
    b = Provider("lastfm", available=lambda s: lastfm.api_key(s) is not None)
    b.api_key = lastfm.api_key
    config(store, {"musicbrainz", "lastfm"})
    w = worker(store, [a,b])
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == []
    row = store.conn.execute("SELECT status FROM enrichment_provider_config WHERE provider='lastfm'").fetchone()
    assert row[0] == "unavailable"
    store.set_setting("lastfm_api_key", "first-key")
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == [tid]
    assert w.drain_once() == 0
    store.set_setting("lastfm_api_key", "second-key")
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == [tid,tid]
    assert "first-key" not in str(list(map(tuple, store.conn.execute("SELECT * FROM enrichment_provider_config"))))
    assert "second-key" not in str(list(map(tuple, store.conn.execute("SELECT * FROM enrichment_provider_config"))))
    assert w.drain_once() == 0


def test_provider_settings_routes_wake_owned_worker(store):
    c = _client(store)
    wake = Mock()
    c.app.state.ctx.enrich_worker.trigger = wake
    response = c.post("/setup/enrichment", data={"order": [p["name"] for p in enrichment.PROVIDERS],
                                               "enabled": ["musicbrainz"]})
    assert response.status_code == 200
    assert wake.call_count == 1
    assert c.post("/settings/lastfm-key", data={"key":"new"}).status_code == 204
    assert wake.call_count == 2


def test_disabled_provider_retains_retry_evidence_without_reporting_an_active_retry(store):
    tid = store.upsert_track("v1", "Song", "Artist", None, 200)
    track = store.catalog_subject(tid)
    store.record_enrichment_attempt(track["identity_key"], "deezer", {}, retry=True, now=1000,
                                    error="timeout")
    assert store.enrichment_retry_status()["count"] == 1
    config(store, set())
    store.reconcile_provider_configuration(enrichment.provider_configuration(store, {"deezer":Provider("deezer")}))
    assert store.enrichment_retry_status()["count"] == 0
    assert store.provider_coverage(track["recording_id"])["deezer"] == "disabled"
    assert store.enrichment_attempts(track["identity_key"])["deezer"]["status"] == "retry"
