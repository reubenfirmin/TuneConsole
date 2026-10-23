"""Slow SQLite calls and serving-lock waits retain separate, sanitized timings."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import logging
import sqlite3
import threading

import pytest

from yt_playlist.core import db_diagnostics as diagnostics
from yt_playlist.core.activity import WorkClosing, cancellable_work
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.repos.base import Repo, synchronized


@pytest.fixture
def clock(monkeypatch, caplog):
    value = [0.]
    monkeypatch.setattr(diagnostics, "monotonic", lambda: value[0])
    caplog.set_level(logging.WARNING, logger="yt_playlist.database")
    return value


class ProbeRepo(Repo):
    @synchronized
    def query(self, sql, values=()):
        return self.conn.execute(sql, values).fetchall()


def test_slow_sql_identifies_operation_phase_and_site_without_values(store, clock, caplog):
    def slow(value):
        clock[0] += diagnostics.SLOW_SQL_SECONDS
        return value

    store.conn.create_function("slow", 1, slow)
    with work_span("trend rollups"):
        result = ProbeRepo(store).query("SELECT slow(?), 'literal-private-title'", ("private-credential",))
    assert result[0][0] == "private-credential"
    message = caplog.text
    assert "Slow SQL" in message and "operation=ProbeRepo.query" in message
    assert "phase=trend rollups" in message and "elapsed=0.250s" in message
    assert "statement=SELECT" in message and "sql_id=" in message
    assert "site=test_db_diagnostics.py:" in message
    assert "private-credential" not in message and "literal-private-title" not in message
    assert "lock wait" not in message


@pytest.mark.parametrize("fetch", ["fetchall", "fetchone", "fetchmany", "iterate"])
def test_fetch_time_accumulates_without_counting_python_time(store, clock, caplog, fetch):
    def scan(value):
        clock[0] += .125
        return value

    store.conn.create_function("scan", 1, scan)
    cursor = store.conn.cursor().execute(
        "WITH values_to_scan(n) AS (VALUES(1),(2),(3),(4)) SELECT scan(n) FROM values_to_scan")
    # Time spent by the caller between fetches is outside the SQL measurement.
    clock[0] += 20
    if fetch == "fetchall":
        rows = cursor.fetchall()
    elif fetch == "fetchmany":
        cursor.arraysize = 2
        rows = cursor.fetchmany(2) + cursor.fetchmany()
    elif fetch == "fetchone":
        rows = [cursor.fetchone() for _ in range(4)]
    else:
        rows = list(cursor)
    assert [row[0] for row in rows] == [1, 2, 3, 4]
    records = [r.message for r in caplog.records if "Slow SQL" in r.message]
    assert len(records) == 1
    expected = {"fetchall": "0.500", "fetchmany": "0.375"}.get(fetch, "0.250")
    assert f"elapsed={expected}s" in records[0]


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_and_cancelled_sql_are_timed_without_exception_contents(store, clock, caplog, cancelled):
    stop = threading.Event()

    def fail():
        clock[0] += 1
        if cancelled:
            stop.set()
            return 1
        raise ValueError("private-provider-error")

    store.conn.create_function("fail", 0, fail)
    with pytest.raises(WorkClosing if cancelled else sqlite3.OperationalError):
        with work_span("first-play refresh"), cancellable_work(stop.is_set):
            ProbeRepo(store).query("""WITH RECURSIVE n(i) AS (
                VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i < 2000)
                SELECT fail() FROM n""")
    assert "outcome=" + ("cancelled" if cancelled else "failed") in caplog.text
    assert "phase=first-play refresh" in caplog.text
    assert "private-provider-error" not in caplog.text
    # A failed statement must not poison later calls or retain its operation context.
    caplog.clear()
    assert store.conn.execute("SELECT 1").fetchone()[0] == 1
    assert not caplog.records


def test_contended_serving_lock_is_not_reported_as_sql_execution(store, clock, caplog, monkeypatch):
    waiting = threading.Event()
    original_enter = store.priority.enter

    def enter():
        release = original_enter()
        waiting.set()
        return release

    with store._lock, ThreadPoolExecutor(1) as pool:
        monkeypatch.setattr(store.priority, "enter", enter)
        future = pool.submit(ProbeRepo(store).query, "SELECT 1")
        try:
            assert waiting.wait(2)
            clock[0] += 1
        finally:
            # The context still owns one acquisition; release only the waiter's blocker here.
            store._lock.release()
        try:
            assert future.result(2)[0][0] == 1
        finally:
            store._lock.acquire()
    assert "Slow database lock wait" in caplog.text
    assert "operation=ProbeRepo.query" in caplog.text and "elapsed=1.000s" in caplog.text
    assert "Slow SQL" not in caplog.text


def test_fast_operations_and_nested_phases_are_quiet(store, clock, caplog):
    with work_span("outer"):
        with work_span("inner"):
            assert diagnostics.current_phase() == "inner"
        assert diagnostics.current_phase() == "outer"
        assert ProbeRepo(store).query("SELECT 1")[0][0] == 1
    assert diagnostics.current_phase() == "unscoped"
    assert not caplog.records


def test_reusing_a_cursor_does_not_combine_separate_fast_queries(store, clock, caplog):
    def short():
        clock[0] += diagnostics.SLOW_SQL_SECONDS - .01
        return 1

    store.conn.create_function("short", 0, short)
    cursor = store.conn.cursor()
    for _ in range(3):
        assert cursor.execute("SELECT short()").fetchone()[0] == 1
    assert not caplog.records


@pytest.mark.parametrize("elapsed,warn", [(.099, False), (.1, True)])
def test_lock_warning_threshold(store, clock, caplog, monkeypatch, elapsed, warn):
    def acquire(*args):
        clock[0] += elapsed
        return False

    monkeypatch.setattr(store._lock, "_acquire", acquire)
    assert not store._lock.acquire(False)
    assert ("Slow database lock wait" in caplog.text) is warn


def test_cancelled_lock_wait_retains_phase_and_releases_priority(store, clock, caplog):
    def stop():
        clock[0] += 1
        raise WorkClosing("private-cancellation-detail")

    with pytest.raises(WorkClosing), work_span("trend rollups"), store.priority.background(stop):
        ProbeRepo(store).query("SELECT 1")
    assert "Slow database lock wait" in caplog.text
    assert "operation=ProbeRepo.query" in caplog.text and "phase=trend rollups" in caplog.text
    assert "outcome=cancelled" in caplog.text and "elapsed=1.000s" in caplog.text
    assert "Slow SQL" not in caplog.text and "private-cancellation-detail" not in caplog.text
    assert store.priority._foreground == 0
    assert ProbeRepo(store).query("SELECT 1")[0][0] == 1


def test_unsuccessful_timed_lock_acquisition_is_identified(store, clock, caplog, monkeypatch):
    def busy(*args):
        clock[0] += 1
        return False

    monkeypatch.setattr(store._lock, "_acquire", busy)
    assert not store._lock.acquire(timeout=1)
    assert "Slow database lock wait" in caplog.text and "outcome=busy" in caplog.text
    assert "Slow SQL" not in caplog.text


@pytest.mark.parametrize("batch", ["executemany", "executescript"])
def test_batch_statements_are_timed_and_private(store, clock, caplog, batch):
    store.conn.execute("CREATE TEMP TABLE diagnostic_batch(value)")

    def slow(value):
        clock[0] += .25
        return value

    store.conn.create_function("slow", 1, slow)
    if batch == "executemany":
        store.conn.executemany("INSERT INTO diagnostic_batch VALUES (slow(?))", [("private-title",)])
    else:
        store.conn.executescript("INSERT INTO diagnostic_batch VALUES (slow('private-title'));")
    assert f"stage={batch}" in caplog.text and "elapsed=0.250s" in caplog.text
    assert "private-title" not in caplog.text


@pytest.mark.parametrize("finish", ["commit", "rollback", "context", "failed_commit"])
def test_transaction_completion_is_timed(store, clock, caplog, monkeypatch, finish):
    store.conn.execute("CREATE TEMP TABLE diagnostic_parent(id PRIMARY KEY)")
    store.conn.execute("""CREATE TEMP TABLE diagnostic_child(parent REFERENCES diagnostic_parent(id)
                       DEFERRABLE INITIALLY DEFERRED)""")
    store.conn.execute("INSERT INTO diagnostic_child VALUES (1)")
    times = iter((0., 1.))
    monkeypatch.setattr(diagnostics, "monotonic", lambda: next(times))
    if finish == "context":
        with pytest.raises(sqlite3.IntegrityError):
            with store.conn:
                pass
    elif finish == "failed_commit":
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.commit()
    elif finish == "rollback":
        store.conn.rollback()
    else:
        # Satisfy the deferred constraint before measuring the successful commit.
        monkeypatch.setattr(diagnostics, "monotonic", lambda: clock[0])
        store.conn.execute("INSERT INTO diagnostic_parent VALUES (1)")
        monkeypatch.setattr(diagnostics, "monotonic", lambda: next(times))
        store.conn.commit()
    monkeypatch.setattr(diagnostics, "monotonic", lambda: clock[0])
    store.conn.rollback()
    assert "stage=transaction" in caplog.text and "elapsed=1.000s" in caplog.text
    outcome = "failed" if finish in {"context", "failed_commit"} else "ok"
    assert f"outcome={outcome}" in caplog.text


def test_maintenance_reader_uses_the_same_sql_diagnostics(store, clock, caplog):
    with store.read_snapshot() as snapshot:
        def slow():
            clock[0] += 1
            return 1
        snapshot.store.conn.create_function("slow", 0, slow)
        assert ProbeRepo(snapshot.store).query("SELECT slow()")[0][0] == 1
    assert "Slow SQL" in caplog.text and "operation=ProbeRepo.query" in caplog.text


@pytest.mark.parametrize('commit_s,level', [(.01, logging.DEBUG), (.4, logging.INFO)])
def test_publication_timings_separate_lock_wait_commit_and_checkpoint(clock, caplog, monkeypatch, commit_s, level):
    cpu = [0.]
    monkeypatch.setattr(diagnostics, 'thread_time', lambda: cpu[0])
    caplog.set_level(logging.DEBUG, logger='yt_playlist.database')

    @contextmanager
    def delayed_lock():
        clock[0] += .2
        yield

    timings = diagnostics.PublicationTimings()
    with work_span('rec: cluster surfaces and PPR'):
        with timings.locked(delayed_lock()):
            with timings.measure('commit'):
                clock[0] += commit_s
                cpu[0] += .001
        with timings.measure('checkpoint'):
            clock[0] += .5
            cpu[0] += .01
        timings.report('published')
    assert len(caplog.records) == 1
    record = caplog.records[0]
    phase, outcome, stages = record.args
    assert phase == 'rec: cluster surfaces and PPR' and outcome == 'published'
    assert record.levelno == level
    assert stages['lock_wait'] == {'wall_s': .2, 'cpu_s': 0.}
    assert stages['lock'] == stages['commit'] == {'wall_s': commit_s, 'cpu_s': .001}
    assert stages['checkpoint'] == {'wall_s': .5, 'cpu_s': .01}
