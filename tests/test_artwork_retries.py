"""Optional artwork failures recover after a completed metadata pass, including after restart."""
from functools import partial
import io
import json
import logging
from types import SimpleNamespace
import urllib.error

import pytest

from yt_playlist.core.store import Store
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.integrations.coverartarchive import CoverArtArchiveIAO, COVER_TTL
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


def install_archive(monkeypatch, clock, fetch):
    def open_request(request, *, timeout):
        return io.StringIO(json.dumps(fetch(request.full_url)))
    iao = CoverArtArchiveIAO(now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
        open_fn=open_request, pacer=SimpleNamespace(wait=lambda: None))
    monkeypatch.setattr(coverart, '_iao', iao)
    return iao


def test_transient_art_failure_recovers_after_restart_without_repeating_complete_metadata(tmp_path, monkeypatch):
    clock = [1000.0]
    calls, recovered = [], [False]
    def fetch(url):
        calls.append(url)
        if not recovered[0]:
            raise TimeoutError("archive offline")
        return {"images":[{"front":True,"image":"https://cover.example/real.jpg"}]}
    install_archive(monkeypatch, clock, fetch)
    path = tmp_path / "art.db"
    store = Store(path); store.init_schema()
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, 200)
    provider = Provider()
    w = worker(store, clock, provider)
    assert w.drain_once() == 1
    assert store.enrichment_reports.coverage_stats()["processed"] == 1
    assert store.enrichment_reports.queue_remaining() == 0
    assert store.enrichment_state.artwork_retry_status() == {"count":1,"retry_at":1060}
    assert len(calls) == 1
    store.conn.close()
    # Simulate a new process: persistent schedule survives, while the response cache does not.
    install_archive(monkeypatch, clock, fetch)
    store = Store(path); store.init_schema()
    w = worker(store, clock, provider)
    clock[0] = 1059
    assert w.drain_once() == 0
    assert len(calls) == 1
    recovered[0] = True
    clock[0] = 1060
    assert w.drain_once() == 1
    assert store.tracks.catalog_subject(tid)["thumbnail"] == "https://cover.example/real.jpg"
    assert provider.calls == [tid]
    assert store.enrichment_state.artwork_retry_status()["count"] == 0
    assert store.conn.execute("SELECT status FROM enrichment_artwork").fetchone()[0] == "found"
    store.conn.close()


def test_cached_failures_retain_retry_outcome_and_due_job_waits_for_cache_expiry(store, monkeypatch):
    clock = [1000.0]
    calls = []
    def fetch(url):
        calls.append(url)
        raise urllib.error.HTTPError(url, 503, "busy", {"Retry-After":"120"}, None)
    install_archive(monkeypatch, clock, fetch)
    provider = Provider()
    store.tracks.upsert_track("v1", "One", "Artist", None, 200)
    w = worker(store, clock, provider)
    assert w.drain_once() == 1
    assert store.enrichment_state.artwork_retry_status()["retry_at"] >= 1120
    clock[0] = 1010
    store.tracks.upsert_track("v2", "Two", "Artist", None, 200)
    assert w.drain_once() == 1
    assert len(calls) == 1                        # second track sees cached failure, not absence
    assert store.enrichment_state.artwork_retry_status()["count"] == 2
    assert store.enrichment_reports.coverage_stats()["processed"] == 2
    assert store.enrichment_reports.queue_remaining() == 0


def test_confirmed_absence_has_no_retry_job(store, monkeypatch):
    def missing(url):
        raise urllib.error.HTTPError(url, 404, "no cover", {}, None)
    install_archive(monkeypatch, [1000], missing)
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, 200)
    assert worker(store, [1000], Provider()).drain_once() == 1
    assert store.conn.execute("SELECT status FROM enrichment_artwork WHERE entry_id=?", (tid,)).fetchone()[0] == "absent"
    assert store.enrichment_state.artwork_retry_status()["count"] == 0


def test_bad_request_is_not_recorded_as_confirmed_absence(store, monkeypatch):
    def bad_request(url):
        raise urllib.error.HTTPError(url, 400, 'invalid UUID', {}, None)
    install_archive(monkeypatch, [1000], bad_request)
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', None, 200)
    assert worker(store, [1000], Provider()).drain_once() == 1
    assert store.enrichment_state.artwork_retry_status() == {'count': 1, 'retry_at': 1060}
    assert store.conn.execute('SELECT status FROM enrichment_artwork WHERE entry_id=?',
                              (tid,)).fetchone()[0] == 'retry'


def test_retry_preserves_useful_cover_added_after_failure(store, monkeypatch):
    from yt_playlist.enrich.artwork import retry_due
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, 200)
    store.enrichment_state.record_artwork_result(tid, ["release"], coverart.CoverResult("retry", retry_in=60), 1000)
    store.tracks.set_track_art(tid, "https://cover.example/user.jpg")
    def unexpected(*args):
        raise AssertionError("useful cover must prevent another archive call")
    monkeypatch.setattr(coverart, "front_cover", unexpected)
    assert retry_due(store, lambda:1060) == 1
    assert store.tracks.catalog_subject(tid)["thumbnail"] == "https://cover.example/user.jpg"
    assert store.enrichment_state.artwork_retry_status()["count"] == 0


def test_stale_cover_applied_by_waterfall_stays_pending_and_refreshes_after_restart(tmp_path, monkeypatch):
    from yt_playlist.enrich.artwork import retry_due
    clock, calls, recovered = [1000.0], [], [False]

    def fetch(url):
        calls.append(url)
        if len(calls) > 1 and not recovered[0]:
            raise TimeoutError('archive offline')
        return {'images': [{'front': True, 'image': 'https://cover.example/retained.jpg'}]}
    install_archive(monkeypatch, clock, fetch)
    assert coverart.lookup(['release']).status == 'found'
    clock[0] += COVER_TTL
    path = tmp_path / 'stale-art.db'
    store = Store(path); store.init_schema()
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', None, 200)
    provider = Provider()
    assert worker(store, clock, provider).drain_once() == 1
    assert store.tracks.catalog_subject(tid)['thumbnail'] == 'https://cover.example/retained.jpg'
    assert store.tracks.artwork_source(tid) == 'coverart'
    assert store.enrichment_state.artwork_retry_status() == {'count': 1, 'retry_at': clock[0] + 60}
    assert len(calls) == 2
    store.conn.close()
    install_archive(monkeypatch, clock, fetch)
    store = Store(path); store.init_schema()
    try:
        clock[0] += 59
        assert retry_due(store, lambda: clock[0]) == 0
        assert len(calls) == 2
        recovered[0] = True
        clock[0] += 1
        assert worker(store, clock, provider).drain_once() == 1
        assert len(calls) == 3 and provider.calls == [tid]
        assert store.enrichment_state.artwork_retry_status()['count'] == 0
        assert store.tracks.catalog_subject(tid)['thumbnail'] == 'https://cover.example/retained.jpg'
    finally:
        store.conn.close()


def test_explicit_user_cover_completes_pending_refresh_even_if_underlying_source_is_archive(store, monkeypatch):
    from yt_playlist.core import catalog
    from yt_playlist.enrich.artwork import retry_due
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', None, 200)
    store.tracks.set_track_art(tid, 'https://cover.example/archive.jpg', source='coverart')
    store.enrichment_state.record_artwork_result(tid, ['release'],
        coverart.CoverResult('retry', retry_in=60), 1000)
    with store._lock, store.conn:
        catalog.choose_field(store.conn, tid, 'thumbnail', 'https://cover.example/chosen.jpg')
    monkeypatch.setattr(coverart, 'front_cover', lambda *a: pytest.fail('User cover needs no archive refresh'))
    assert retry_due(store, lambda: 1060) == 1
    assert store.enrichment_state.artwork_retry_status()['count'] == 0
    assert store.tracks.artwork_source(tid) == 'user'
    assert store.tracks.catalog_subject(tid)['thumbnail'] == 'https://cover.example/chosen.jpg'
