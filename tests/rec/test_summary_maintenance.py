"""Normal worker maintenance owns recap and cleanup freshness, including retries."""
from datetime import datetime, timezone
import logging
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tests.conftest import seed_catalog_track
from yt_playlist.core.store import Store
from yt_playlist.library import analysis
from yt_playlist.rec import actions, maintenance, trend_rollups
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app


def timestamp(month):
    return datetime.fromisoformat(month + '-10').replace(tzinfo=timezone.utc).timestamp()


def worker_for(store, monkeypatch, now):
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: now[0],
        logger=logging.getLogger(__name__)), debounce_s=0)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    return worker


def tick(worker):
    worker.projections.poll()
    worker._run_pending()


def seed(store):
    iid = store.identities.upsert_identity('Listener', 'fixture', None, True)
    seed_catalog_track(store, 'song', 'Song', 'Artist', video_id='video', genre='Techno')
    for month in ('2026-08', '2026-09', '2026-10'):
        store.history.record_history_plays(iid, timestamp(month), ['song'], source_timezone='UTC')
    return iid


def test_cold_jobs_coalesce_with_learning_and_publish_recap_route_and_cleanup(store, monkeypatch):
    iid = seed(store)
    store.playlists.upsert_playlist(iid, 'empty', 'Empty playlist', 0, '', 0)
    now = [timestamp('2026-10')]
    worker = worker_for(store, monkeypatch, now)
    rebuilds = []
    monkeypatch.setattr(worker, '_do_rebuild', lambda: rebuilds.append(True))
    worker.trigger()
    worker.projections.poll()
    worker.projections.poll()
    assert worker.jobs.has('trends') and worker.jobs.has('cleanup')
    worker._run_pending()
    assert rebuilds == [True]
    assert store.rec_proposals.get_proposals('trend_rollups')['story']['month'] == '2026-09'
    card = next(c for c in actions.take_action(store, now[0], {}) if c.kind == 'cleanup')
    assert card.badge == '1'
    app = create_app(store, lambda: {}, now_fn=lambda: now[0])
    with TestClient(app) as client:
        response = client.get('/trends/story/2026-09')
        assert response.status_code == 200
        assert 'September' in response.text
    # Reading the recap updates a display stamp, not a new source generation.
    worker.projections.retry_at.clear()
    worker.projections.poll()
    assert not worker.busy


def test_source_changes_refresh_only_relevant_jobs_and_month_rollover(store, monkeypatch):
    iid = seed(store)
    now = [timestamp('2026-10')]
    worker = worker_for(store, monkeypatch, now)
    tick(worker)
    worker.projections.retry_at.clear()
    before = worker.projections.completed.copy()
    store.player_events.record_player_event(iid, 'progress', 'video', 30, 200, None, '{}', now[0])
    worker.projections.poll()
    assert not worker.busy  # telemetry must not launch library-wide scans
    store.history.record_history_plays(iid, now[0] + 86400, ['song'], source_timezone='UTC')
    worker.projections.poll()
    assert worker.jobs.has('trends') and not worker.jobs.has('cleanup')
    worker._run_pending()
    assert worker.projections.completed['trends'] != before['trends']
    assert worker.projections.completed['cleanup'] == before['cleanup']
    worker.projections.retry_at.clear()
    store.playlists.upsert_playlist(iid, 'new-empty', 'Synced empty playlist', 0, '', now[0])
    tick(worker)
    assert store.rec_proposals.get_proposals('cleanup')['count'] == 1
    worker.projections.retry_at.clear()
    now[0] = timestamp('2026-11')
    worker.projections.poll()
    assert worker.jobs.has('trends') and not worker.jobs.has('cleanup')
    worker._run_pending()
    assert store.rec_proposals.get_proposals('trend_rollups')['story']['month'] == '2026-10'


@pytest.mark.parametrize('kind', ['trends', 'cleanup'])
def test_conflicting_publication_retries_with_backoff_and_keeps_last_good(store, monkeypatch, kind):
    iid = seed(store)
    now = [timestamp('2026-10')]
    worker = worker_for(store, monkeypatch, now)
    tick(worker)
    surface = maintenance.SURFACES[kind]
    saved = store.rec_proposals.get_proposals(surface)
    worker.projections.completed.pop(kind)
    worker.projections.retry_at.clear()
    owner, name = (trend_rollups, '_calculate') if kind == 'trends' else (analysis, 'cleanup_summary')
    original = getattr(owner, name)

    def conflict(reader, *args):
        assert reader.conn is not store.conn
        result = original(reader, *args)
        store.playlists.upsert_playlist(iid, 'race', 'Changed during calculation', 0, '', now[0])
        return result

    monkeypatch.setattr(owner, name, conflict)
    tick(worker)
    assert store.rec_proposals.get_proposals(surface) == saved
    assert worker.projections.failures[kind] == 1
    assert worker.projections.retry_at[kind] > maintenance.time.monotonic()
    worker.projections.poll()
    assert kind not in worker.projections.active
    monkeypatch.setattr(owner, name, original)
    worker.projections.retry_at.clear()
    tick(worker)
    assert worker.projections.failures[kind] == 0
    assert store.rec_proposals.get_proposals('cleanup')['count'] == 1


def test_restart_and_external_import_are_not_lost_and_shutdown_stops_admission(tmp_path, monkeypatch):
    path = tmp_path / 'summaries.db'
    store = Store(path)
    store.init_schema()
    iid = seed(store)
    now = [timestamp('2026-10')]
    worker = worker_for(store, monkeypatch, now)
    tick(worker)
    with sqlite3.connect(path) as writer:
        writer.execute("INSERT INTO settings VALUES ('takeout_imported_at','123')")
    worker.projections.retry_at.clear()
    worker.projections.poll()
    assert worker.jobs.has('trends') and worker.jobs.has('cleanup')
    worker._run_pending()
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        store.playlists.upsert_playlist(iid, 'offline-empty', 'Offline change', 0, '', now[0])
        worker = worker_for(store, monkeypatch, now)
        tick(worker)
        assert store.rec_proposals.get_proposals('cleanup')['count'] == 1
        worker.shutdown()
        worker.projections.completed.clear()
        worker.projections.poll()
        assert not worker.busy
    finally:
        store.conn.close()


def test_full_rebuild_failure_does_not_swallow_summary_jobs(store, monkeypatch):
    seed(store)
    worker = worker_for(store, monkeypatch, [timestamp('2026-10')])

    def fail():
        raise RuntimeError('learning temporarily unavailable')

    monkeypatch.setattr(worker, '_do_rebuild', fail)
    # Avoid waiting for the unrelated learning retry clock in this focused check.
    waits = iter((False, True))
    monkeypatch.setattr(worker._stop, 'wait', lambda timeout: next(waits))
    worker.trigger()
    tick(worker)
    assert store.rec_proposals.get_proposals('trend_rollups')['story']
    assert store.rec_proposals.get_proposals('cleanup') == {'count': 0, 'thumbnails': []}
    assert not worker.projections.active


@pytest.mark.parametrize('analytics_enabled', [False, True])
def test_poll_shares_one_scoped_capture_despite_unrelated_key_churn(store, monkeypatch, analytics_enabled):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS', '1' if analytics_enabled else '0')
    seed(store)
    worker = worker_for(store, monkeypatch, [timestamp('2026-10')])
    captures = []
    original = store._snapshot_revisions.capture

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captures.append(result)
        return result

    monkeypatch.setattr(store._snapshot_revisions, 'capture', capture)
    worker.projections.poll()
    assert len(captures) == 1
    baseline = captures.pop()
    assert baseline
    assert all(not key or (table, key) in set().union(*maintenance.DEPENDENCIES.values())
               for table, key in baseline)
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',
                               [(f'unrelated-{i}', 'value') for i in range(4000)])
        store.conn.execute("DELETE FROM settings WHERE key LIKE 'unrelated-%'")
    assert len(original()) > 4000
    worker.projections.poll()  # Coalesced jobs are still active on this idle tick.
    assert captures == [baseline]
    expected = {'trends', 'cleanup'} | ({'analytics'} if analytics_enabled else set())
    assert worker.projections.active == expected


def test_local_import_setting_invalidates_only_trends_inputs(store):
    now = timestamp('2026-10')
    before = {kind: maintenance.inputs(store, kind, now) for kind in maintenance.SURFACES}
    store.settings.set_setting('unrelated', 'new')
    assert {kind: maintenance.inputs(store, kind, now) for kind in maintenance.SURFACES} == before
    store.settings.set_setting('takeout_imported_at', '123')
    assert maintenance.inputs(store, 'trends', now) != before['trends']
    assert maintenance.inputs(store, 'cleanup', now) == before['cleanup']
