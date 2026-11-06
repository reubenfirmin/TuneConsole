from fastapi.testclient import TestClient
import pytest

from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app
from yt_playlist.web.context import Ctx
from tests.conftest import FakeClient
from yt_playlist.rec import home_prepared
from yt_playlist.rec.worker_queue import Job


class _ArtistClient(FakeClient):
    """A client that answers the outward-discovery fetch with one new + one owned album."""
    def search(self, query, filter="songs"):
        return [{"browseId": "ART1"}]

    def get_artist(self, browse_id):
        return {"name": "Fav", "description": None, "thumbnails": None, "subscribers": None,
                "albums": {"results": [
                    {"title": "Brand New LP", "year": "2024", "browseId": "ALB_NEW", "thumbnails": None},
                    {"title": "Owned Record", "year": "2010", "browseId": "ALB_OLD", "thumbnails": None},
                ]}}


def _ctx(store, client):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    return iid, Ctx(store=store, client_provider=lambda: {iid: client}, now_fn=lambda: 1.0,
                    templates=None, jobs=None)


def test_discovery_waits_for_priority_recommendation_retirement(store, monkeypatch):
    from yt_playlist.rec import discover
    _, ctx = _ctx(store, FakeClient())
    worker = RecWorker(ctx)
    calls = []
    monkeypatch.setattr(discover, 'run_discovery', lambda *args: calls.append('discovery') or {})
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    store._recommendation_retirement_pending = True
    assert worker._discover(10) is None
    assert not calls and not worker.jobs
    store._recommendation_retirement_pending = False
    worker._discover(11)
    assert calls == ['discovery']
    worker.shutdown()


def test_worker_materializes_proposals(store, monkeypatch):
    iid, ctx = _ctx(store, _ArtistClient())
    t = store.tracks.upsert_track("v1", "Song", "Fav", None, None)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "P", "P", 1, "h", 0.0), [t])
    store.history.record_history_plays(iid, 1.0, ["song|fav"], source_timezone="UTC")

    worker = RecWorker(ctx)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    worker._discover(1.0)
    worker.shutdown()
    # discovery now accumulates into the pool (scan-ledger backed), not the overwrite proposals
    titles = {a["title"] for a in store.discovery.get_discovered_albums()}
    assert "Brand New LP" in titles        # the new album was scanned into the pool


@pytest.mark.parametrize('predecessors', [False, True])
def test_home_discover_serves_cached(store, monkeypatch, predecessors):
    from tests.test_growth_journey import early_journey

    iid = store.accounts.upsert_account("main", "cred", None, True)
    if predecessors:
        early_journey(store)
    store.discovery.upsert_discovered_album("B", "X", "Cached LP", "2024", None, now=1.0)
    prepared = home_prepared.prepare_discovery(store, 1000.0)
    assert any(album['title'] == 'Cached LP' for album in prepared['discovery_albums'])

    def unexpected_fetch(*args, **kwargs):
        pytest.fail('A cached discovery request must not fetch or prepare its content')

    fake = FakeClient()
    monkeypatch.setattr(fake, 'search', unexpected_fetch)
    monkeypatch.setattr(fake, 'get_artist', unexpected_fetch)
    monkeypatch.setattr(home_prepared, 'prepare', unexpected_fetch)
    monkeypatch.setattr(home_prepared, 'prepare_discovery', unexpected_fetch)
    with TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: 1000.0,
                               background_work=False), base_url="http://127.0.0.1") as client:
        response = client.get('/home/discover')
        if predecessors:
            assert response.status_code == 200 and 'Cached LP' in response.text
        else:
            assert response.status_code == 409
            assert 'This stop is still ahead on your journey' in response.text


def test_discovery_pass_publishes_new_songs_before_the_next_rebuild(store, monkeypatch):
    from yt_playlist.rec import discover, fresh_pool

    _, ctx = _ctx(store, FakeClient())
    monkeypatch.setattr(discover, "new_artists", lambda ctx: [])

    def populate(ctx, now):
        for i in range(12):
            store.discovery.upsert_discovered_track(f"new {i}|artist {i}", f"v{i}", f"New {i}",
                f"Artist {i}", None, None, "Jazz", "2020", "radio:seed", now)

    monkeypatch.setattr(discover, "populate_discovered_tracks", populate)
    monkeypatch.setattr(discover, "populate_radio_tracks", lambda *a: None)
    worker = RecWorker(ctx)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    try:
        worker._discover(10)
        # Discovery persists new inputs, then joins the admitted ranking round.
        # Only that producer can make the candidates available to serving.
        assert len(store.discovery.discovered_track_keys()) == 12
        assert fresh_pool.pick(store, 11) == []
        assert worker.jobs.has(Job.BUNDLES) and not worker.jobs.has(Job.HOME)
        assert worker._refresh_bundles() is not None
        assert len(fresh_pool.pick(store, 11)) == 12
        assert worker.jobs.has(Job.HOME)
    finally:
        worker.shutdown()


@pytest.mark.parametrize("fails", [False, True])
def test_discovery_finishes_and_queues_candidate_refresh_only_on_success(store, monkeypatch, fails):
    from yt_playlist.rec import discover
    _, ctx = _ctx(store, FakeClient())
    worker = RecWorker(ctx)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)

    def scan(*args):
        assert worker.discovery_busy and not worker.busy
        if fails:
            raise RuntimeError("Offline")
        return {}

    monkeypatch.setattr(discover, "run_discovery", scan)
    if fails:
        with pytest.raises(RuntimeError, match="Offline"):
            worker._discover(10)
    else:
        worker._discover(10)
    assert not worker._discovering
    assert worker.jobs.has(Job.BUNDLES) is (not fails)
    assert not worker.jobs.has(Job.HOME)
    assert worker.discovery_busy is (not fails)
    worker.shutdown()


def test_discovery_retries_an_actual_sql_deadline_without_an_exception_trace(
        file_backed_store, monkeypatch, caplog):
    import logging
    import sqlite3
    from yt_playlist.core.activity import work_budget
    from yt_playlist.rec import discover

    store = file_backed_store
    _, ctx = _ctx(store, FakeClient())
    worker = RecWorker(ctx)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    waits, connections = [], []
    attempts = 0

    def wait(delay):
        waits.append(delay)
        return len(waits) == 3

    def scan(*args):
        nonlocal attempts
        attempts += 1
        assert worker.discovery_busy
        if attempts == 1:
            with store.read_view() as reader:
                connections.append(reader.conn)
                with work_budget(.005):
                    reader.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                        'SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
        assert not worker.jobs.has(Job.BUNDLES), 'A failed discovery must not queue candidate preparation'
        assert not worker.jobs.has(Job.HOME), 'Discovery must not publish a Home menu directly'
        return {}

    monkeypatch.setattr(worker._stop, 'wait', wait)
    monkeypatch.setattr(discover, 'run_discovery', scan)
    with caplog.at_level(logging.INFO):
        worker._tick_loop()
    assert attempts == 2
    assert waits == [1800, 60, 1800]
    assert worker.jobs.has(Job.BUNDLES) and not worker._discovering
    assert not worker.jobs.has(Job.HOME)
    assert 'Discovery deferred by read deadline' in caplog.text
    assert not any(record.exc_info for record in caplog.records)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')
    # A failed private read must release every reader slot for the next pass.
    for _ in range(4):
        assert store._analytics_readers.acquire(blocking=False)
    for _ in range(4):
        store._analytics_readers.release()
    worker.shutdown()


@pytest.mark.parametrize('failure', ['closing', 'unexpected', 'policy-capacity'])
def test_discovery_ticker_distinguishes_shutdown_from_unexpected_failures(store, monkeypatch, caplog, failure):
    import time
    from yt_playlist.core.activity import WorkClosing
    from yt_playlist.repos.policy_storage import PolicyCapacityFull

    _, ctx = _ctx(store, FakeClient())
    worker = RecWorker(ctx)
    waits = []
    rejected_at = []

    def wait(delay):
        waits.append(delay)
        return len(waits) == 2

    def fail(now):
        if failure == 'policy-capacity':
            rejected_at.append(time.monotonic())
            raise PolicyCapacityFull(131000, 60000000, 1024, 500000)
        raise (WorkClosing('Stopping') if failure == 'closing' else RuntimeError('Unexpected discovery failure'))

    monkeypatch.setattr(worker._stop, 'wait', wait)
    monkeypatch.setattr(worker, '_discover', fail)
    import logging
    with caplog.at_level(logging.INFO):
        worker._tick_loop()
    failures = [record for record in caplog.records if record.message == 'discovery tick failed']
    assert waits == ([1800] if failure == 'closing' else [1800, 1800])
    assert len(failures) == int(failure == 'unexpected')
    if failures:
        assert failures[0].exc_info[0] is RuntimeError
    if failure == 'policy-capacity':
        assert worker.jobs.retry['bundles'].dirty
        assert worker.jobs.retry['bundles'].failures == 0
        assert worker.jobs.retry['bundles'].retry_at >= rejected_at[0] + 60
        assert not worker.jobs.has('home')
        assert 'batch adds 1024 variants' in caplog.text
        assert not any(record.exc_info for record in caplog.records)
    worker.shutdown()


