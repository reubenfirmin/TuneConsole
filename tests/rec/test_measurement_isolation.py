"""A slow policy fingerprint must not serialize other UI database work."""
from concurrent.futures import ThreadPoolExecutor
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.rec import measurement, rec_params, scorecard
from yt_playlist.rec.rec_worker import RecWorker


def test_ui_mutation_completes_during_fingerprint_and_offer_keeps_original_policy(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    started, release = threading.Event(), threading.Event()
    fingerprint = measurement._vector_identity

    def slow_fingerprint(rows):
        started.set()
        assert release.wait(5)
        return fingerprint(rows)

    monkeypatch.setattr(measurement, "_vector_identity", slow_fingerprint)
    with ThreadPoolExecutor(2) as threads:
        future = threads.submit(measurement.record_recommendations, store, "home",
                                [{"key": "song|artist"}], 100)
        try:
            assert started.wait(5)
            threads.submit(rec_params.set_param, store, "axis_weight_cap", 2).result(2)
            assert not future.done()
        finally:
            release.set()
        future.result(5)
    row = store.conn.execute("SELECT id FROM rec_served_impressions").fetchone()
    trace = scorecard.served_recommendation(store, row[0])
    assert trace["configuration"]["params"]["axis_weight_cap"] == 1.4
    assert rec_params.get_param(store, "axis_weight_cap") == 2
    store.conn.close()


def test_measurement_propagates_shutdown_instead_of_swallowing_it(store, monkeypatch):
    def stop(*args, **kwargs):
        raise WorkClosing("shutdown")

    monkeypatch.setattr(measurement, "serving_policy", stop)
    with pytest.raises(WorkClosing, match="shutdown"):
        measurement.record_recommendations(store, "home", [{"key": "song|artist"}], 100)
    assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == 0


def test_queued_offer_returns_before_fingerprint_and_retains_original_state(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: 100), debounce_s=0)
    monkeypatch.setattr(worker, "_start_loop", lambda: None)
    fingerprint = measurement._vector_identity
    calls = []

    def fingerprint_on_worker(rows):
        calls.append(threading.get_ident())
        return fingerprint(rows)

    monkeypatch.setattr(measurement, "_vector_identity", fingerprint_on_worker)
    item = {"key": "song|artist", "score": 3}
    try:
        measurement.record_recommendations(store, "home", [item], 100)
        assert not calls
        assert len(worker.jobs.measurements) == 1
        assert not worker.busy
        rec_params.set_param(store, "axis_weight_cap", 2)
        item["score"] = 999
        with ThreadPoolExecutor(1) as thread:
            thread.submit(worker._flush_measurements).result(5)
        assert calls and all(ident != threading.get_ident() for ident in calls)
        row = store.conn.execute("SELECT id FROM rec_served_impressions").fetchone()
        trace = scorecard.served_recommendation(store, row[0])
        assert trace["configuration"]["params"]["axis_weight_cap"] == 1.4
        assert trace["score"] == 3
        assert not worker.jobs.measurements
    finally:
        worker.shutdown()
        store.conn.close()


def test_queue_is_bounded_and_shutdown_closes_all_pinned_readers(tmp_path, monkeypatch, caplog):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: 100))
    monkeypatch.setattr(worker, "_start_loop", lambda: None)
    readers = []
    capture = measurement.CapturedOffer.__init__

    def track_reader(offer, *args, **kwargs):
        capture(offer, *args, **kwargs)
        readers.append(offer.snapshot.store.conn)

    monkeypatch.setattr(measurement.CapturedOffer, "__init__", track_reader)
    try:
        for _ in range(65):
            measurement.record_recommendations(store, "home", [{"key": "song|artist"}], 100)
        assert len(worker.jobs.measurements) == 64
        assert "backlog full" in caplog.text
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            readers[-1].execute("SELECT 1")
        worker.shutdown()
        for reader in readers:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                reader.execute("SELECT 1")
        measurement.record_recommendations(store, "home", [{"key": "song|artist"}], 100)
        assert not worker.jobs.measurements
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            readers[-1].execute("SELECT 1")
    finally:
        worker.shutdown()
        store.conn.close()


def test_existing_worker_processes_measurement_without_holding_up_response(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db")
    store.init_schema()
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: 100), debounce_s=0)
    started, release = threading.Event(), threading.Event()
    fingerprint = measurement._vector_identity

    def slow(rows):
        started.set()
        assert release.wait(5)
        return fingerprint(rows)

    monkeypatch.setattr(measurement, "_vector_identity", slow)
    with ThreadPoolExecutor(1) as ui:
        try:
            ui.submit(measurement.record_recommendations, store, "home",
                      [{"key": "song|artist"}], 100).result(2)
            assert started.wait(2)
            ui.submit(store.settings.set_setting, "interactive", "saved").result(2)
            assert not worker.busy
            release.set()
            worker.join(3)
            assert store.conn.execute("SELECT COUNT(*) FROM rec_served_impressions").fetchone()[0] == 1
        finally:
            release.set()
            worker.shutdown()
            worker.join(5)
            store.conn.close()
