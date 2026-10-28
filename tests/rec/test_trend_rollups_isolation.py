"""Maintenance must not monopolize the database used by interactive requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from tests.conftest import seed_catalog_track
from yt_playlist.core.activity import Activity
from yt_playlist.core.store import Store
from yt_playlist.rec import trend_rollups
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.repos.trends import TrendsRepo


@pytest.fixture(params=["memory", "file"])
def database(request, tmp_path):
    store = Store(":memory:" if request.param == "memory" else tmp_path / "state.db")
    store.init_schema()
    store.identities.upsert_identity("me", "c", None, True)
    seed_catalog_track(store, "song", "Song", "Artist", video_id="video")
    store.history.record_history_plays(1, 5 * 86400, ["song"], source_timezone="UTC")
    yield store
    store.conn.close()


def test_slow_rollup_sql_allows_ui_reads_and_writes_before_shutdown(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    store.trends.upsert_first_play_min([("track", "saved", 1, 86400, "saved")])
    store.rec_proposals.put_proposals("trend_rollups", {"saved": True})
    entered = threading.Event()
    reader_connections = []
    work = Activity()
    worker = RecWorker(SimpleNamespace(store=store, work=work,
        logger=logging.getLogger(__name__), now_fn=lambda: 86400), debounce_s=0)

    def slow_query(repo):
        reader_connections.append(repo.conn)
        repo.conn.create_function("query_started", 0, lambda: (entered.set(), 0)[1])
        repo.conn.execute("WITH RECURSIVE slow(n) AS (VALUES(query_started()) UNION ALL "
                          "SELECT n+1 FROM slow WHERE n<1000000000) SELECT sum(n) FROM slow").fetchone()

    monkeypatch.setattr(TrendsRepo, "_rebuild_artist_first_play", slow_query)
    monkeypatch.setattr(worker, "_rebuild", worker._refresh_trends)

    async def ui_interaction():
        # Deliberately execute these synchronous repository calls on an event loop, as some
        # existing async handlers do. A blocked shared lock would freeze that loop too.
        assert store.trends.first_play_map("track") == {"saved": 1}
        assert store.rec_proposals.get_proposals("trend_rollups") == {"saved": True}
        store.settings.set_setting("interactive-write", "saved")
        await asyncio.sleep(0)
        return store.settings.get_setting("interactive-write")

    with ThreadPoolExecutor(1) as ui:
        try:
            worker.trigger()
            assert entered.wait(5)
            assert reader_connections[0] is not store.conn
            assert ui.submit(asyncio.run, ui_interaction()).result(2) == "saved"
            assert worker.busy  # the slow query is still running, not just finished quickly
            work.close_admission()
            worker.shutdown()
            worker.join(3)
            assert work.wait(0)
            assert store.trends.first_play_map("track") == {"saved": 1}
            assert store.rec_proposals.get_proposals("trend_rollups") == {"saved": True}
            assert not store.conn.in_transaction
        finally:
            work.close_admission()
            worker.shutdown()
            worker.join(5)
            store.conn.close()


def test_source_reset_during_calculation_cannot_publish_stale_rollup(database, monkeypatch):
    store = database
    store.rec_proposals.put_proposals("trend_rollups", {"saved": True})
    calculate = trend_rollups._calculate

    def reset_during_calculation(reader, now):
        payload = calculate(reader, now)
        store.history.reset_play_history(1)
        return payload

    monkeypatch.setattr(trend_rollups, "_calculate", reset_during_calculation)
    assert trend_rollups.build(store, 6 * 86400) is None
    assert not store.trends.first_play_map("track")
    assert store.rec_proposals.get_proposals("trend_rollups") == {"saved": True}
    assert not store.conn.in_transaction
    monkeypatch.setattr(trend_rollups, "_calculate", calculate)
    assert trend_rollups.build(store, 6 * 86400)["story"] is None


def test_failed_publication_keeps_previous_index_rollup_and_review_stamp(database):
    store = database
    store.trends.upsert_first_play_min([("track", "saved", 1, 86400, "saved")])
    store.rec_proposals.put_proposals("trend_rollups", {"saved": True})
    store.settings.set_setting("trend_spotlight_review_month", "saved")
    with store.conn:
        store.conn.execute("CREATE TRIGGER reject_rollup BEFORE INSERT ON rec_proposals "
                           "WHEN NEW.surface='trend_rollups' "
                           "BEGIN SELECT RAISE(ABORT,'rejected publication'); END")
    with pytest.raises(sqlite3.IntegrityError, match="rejected publication"):
        trend_rollups.build(store, 6 * 86400)
    assert store.trends.first_play_map("track") == {"saved": 1}
    assert store.rec_proposals.get_proposals("trend_rollups") == {"saved": True}
    assert store.settings.get_setting("trend_spotlight_review_month") == "saved"
    assert not store.conn.in_transaction


def test_reader_cannot_modify_source_and_retains_its_input_snapshot(database):
    store = database
    store.settings.set_setting("input", "old")
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        with pytest.raises(sqlite3.DatabaseError):
            reader.settings.set_setting("input", "unauthorized")
        store.settings.set_setting("input", "new")
        assert reader.settings.get_setting("input") == "old"
        assert snapshot.publish(lambda: pytest.fail("stale snapshot published")) is False
    assert store.settings.get_setting("input") == "new"


def test_other_connection_commit_is_detected_even_without_primary_connection_changes(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.init_schema()
    try:
        with store.read_snapshot() as snapshot:
            with sqlite3.connect(path) as writer:
                writer.execute("INSERT INTO settings(key,value) VALUES ('external','changed')")
            assert snapshot.publish(lambda: pytest.fail("stale external snapshot published")) is False
        assert store.settings.get_setting("external") == "changed"
    finally:
        store.conn.close()
