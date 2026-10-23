"""Slow SQLite calls and serving-lock waits retain separate, sanitized timings."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_diagnostics as diagnostics
from yt_playlist.core import priority
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


def test_deliberate_yields_are_separate_from_lock_contention(store, clock, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="yt_playlist.database")
    monkeypatch.setattr(priority, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def wait(_):
        # A scheduler timeslice may slightly overshoot its 0.1s deadline.
        clock[0] += .125
    monkeypatch.setattr(store.priority._condition, 'wait', wait)
    release = store.priority.enter()
    checkpoint = lambda: store.priority.checkpoint(lambda: False)
    try:
        with store.priority.background(checkpoint), work_span('enrichment'):
            for _ in range(12):
                assert ProbeRepo(store).query('SELECT 1')[0][0] == 1
    finally:
        release()
    store._lock._diagnostics.flush(force=True)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    detail = [r.message for r in caplog.records if r.message.startswith('Database lock acquisition:')]
    assert len(detail) == 12
    assert all('wait=0.000s yield=0.125s' in msg for msg in detail)
    summary = [r.message for r in caplog.records if r.message.startswith('Database lock summary:')]
    assert len(summary) == 1
    assert 'operation=ProbeRepo.query phase=enrichment outcome=ok count=12' in summary[0]
    assert 'wait_total=0.000s wait_max=0.000s yield_total=1.500s yield_max=0.125s' in summary[0]


def test_background_polling_and_yield_times_are_measured_independently(store, clock, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="yt_playlist.database")
    attempts = []
    class BusyLock:
        def acquire(self, blocking):
            attempts.append(blocking)
            return len(attempts) == 3
        def release(self):
            pass
    monkeypatch.setattr(store._lock, '_lock', BusyLock())
    monkeypatch.setattr(priority, 'time', SimpleNamespace(monotonic=lambda: clock[0],
        sleep=lambda _: clock.__setitem__(0, clock[0] + .125)))
    def checkpoint():
        with diagnostics.cooperative_lock_yield():
            clock[0] += .125
    with work_span('enrichment'), store.priority.background(checkpoint):
        with store._lock:
            pass
    assert attempts == [False, False, False]
    assert 'elapsed=0.250s yield=0.375s outcome=ok background=True' in caplog.text
    assert 'phase=enrichment' in caplog.text


def test_background_waits_are_bounded_with_accurate_summaries_and_worse_waits(clock, caplog):
    caplog.set_level(logging.DEBUG, logger="yt_playlist.database")
    recorder = diagnostics.LockWaitDiagnostics(interval=10)
    with diagnostics.database_operation('ProviderRepo.publish'), work_span('enrichment'):
        for _ in range(20):
            recorder.record(.125, .25, 'ok', background=True)
        # New, materially worse contention is visible without waiting for a summary.
        recorder.record(.5, .125, 'ok', background=True)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        assert 'elapsed=0.125s' in warnings[0].message
        assert 'elapsed=0.500s' in warnings[1].message
        clock[0] = 10
        recorder.flush()
        summaries = [r.message for r in caplog.records if r.message.startswith('Database lock summary:')]
        assert len(summaries) == 1
        assert 'interval=10.0s operation=ProviderRepo.publish phase=enrichment' in summaries[0]
        assert 'count=21 wait_total=3.000s wait_max=0.500s yield_total=5.125s yield_max=0.250s' in summaries[0]
        assert not recorder.groups
        recorder.record(.125, 0, 'ok', background=True)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3


def test_background_summary_state_is_bounded_for_many_operations(clock, caplog):
    caplog.set_level(logging.INFO, logger="yt_playlist.database")
    recorder = diagnostics.LockWaitDiagnostics(max_groups=3)
    for i in range(100):
        with diagnostics.database_operation(f'operation{i}'), work_span(f'phase{i}'):
            recorder.record(.125, .25, 'ok', background=True)
    assert len(recorder.groups) == 4  # Three named groups and one shared overflow group.
    assert sum(s['count'] for s in recorder.groups.values()) == 100
    recorder.flush(force=True)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 4
    assert len([r for r in caplog.records if 'Database lock summary:' in r.message]) == 4
    assert any('operation=other phase=other outcome=ok count=97' in r.message for r in caplog.records)


def test_foreground_waits_and_background_failures_are_never_aggregated(clock, caplog):
    recorder = diagnostics.LockWaitDiagnostics()
    for _ in range(3):
        recorder.record(.125, 0, 'ok', background=False)
        recorder.record(.001, .125, 'failed', background=True)
        recorder.record(0, .125, 'cancelled', background=True)
    assert len(caplog.records) == 9
    assert all(r.levelno == logging.WARNING for r in caplog.records)
    assert not recorder.groups


@pytest.mark.parametrize('error', [WorkClosing, RuntimeError])
def test_checkpoint_failure_retains_outcome_and_restores_timing_context(store, clock, caplog, error):
    def stop():
        with diagnostics.cooperative_lock_yield():
            clock[0] += .125
            raise error('private failure detail')
    with pytest.raises(error), work_span('enrichment'), store.priority.background(stop):
        ProbeRepo(store).query('SELECT 1')
    assert 'elapsed=0.000s yield=0.125s' in caplog.text
    assert f"outcome={'cancelled' if error is WorkClosing else 'failed'}" in caplog.text
    assert 'operation=ProbeRepo.query phase=enrichment' in caplog.text
    assert 'private failure detail' not in caplog.text
    assert diagnostics._lock_timing.get() is None
    assert store.priority._foreground == 0
    assert ProbeRepo(store).query('SELECT 1')[0][0] == 1


def test_nested_background_acquisition_does_not_yield_while_lock_is_held(store, clock, caplog, monkeypatch):
    monkeypatch.setattr(priority, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def wait(_):
        clock[0] += .125
    monkeypatch.setattr(store.priority._condition, 'wait', wait)
    release = store.priority.enter()
    try:
        with store.priority.background(lambda: store.priority.checkpoint(lambda: False)):
            with store._lock:
                after_outer = clock[0]
                with store._lock:
                    assert clock[0] == after_outer
                    assert store.priority._held.depth == 2
                assert store.priority._held.depth == 1
    finally:
        release()
    assert store.priority._held.depth == 0
    assert not caplog.records


def test_live_log_window_separates_scheduler_yields_and_real_contention(store, tmp_path, caplog):
    """Exercise real condition waits/RLock contention and inspect a fresh, timestamped log."""
    from yt_playlist.core.activity import check_stopping
    caplog.set_level(logging.DEBUG, logger='yt_playlist.database')
    path = tmp_path / 'serving-lock.log'
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    diagnostics.logger.addHandler(handler)
    started = datetime.now().astimezone().isoformat(timespec='milliseconds')
    try:
        release_request = store.priority.enter()
        try:
            with store.priority.background(check_stopping), work_span('live probe: scheduler'):
                for _ in range(5):
                    assert ProbeRepo(store).query('SELECT 1')[0][0] == 1
        finally:
            release_request()
        store._lock._diagnostics.flush(force=True)
        yielded_log = path.read_text()
        assert 'Slow database lock wait' not in yielded_log
        assert 'phase=live probe: scheduler outcome=ok count=5' in yielded_log

        for background in (True, False):
            owned, release_owner = threading.Event(), threading.Event()
            def owner():
                with store.priority.background(check_stopping), store._lock:
                    owned.set()
                    assert release_owner.wait(3)
            def acquire():
                assert owned.wait(3)
                if background:
                    with store.priority.background(check_stopping), work_span('live probe: background contention'):
                        return ProbeRepo(store).query('SELECT 1')
                with work_span('live probe: foreground contention'):
                    return ProbeRepo(store).query('SELECT 1')
            # An owner and a waiter share the actual serving lock. A separate timer frees it
            # after a measurable interval, including for the blocking foreground acquisition.
            with ThreadPoolExecutor(2) as pool:
                owner_future = pool.submit(owner)
                assert owned.wait(3)
                timer = threading.Timer(.2, release_owner.set)
                timer.start()
                try:
                    assert pool.submit(acquire).result(3)[0][0] == 1
                finally:
                    release_owner.set()
                    timer.cancel()
                    timer.join()
                owner_future.result(3)
        store._lock._diagnostics.flush(force=True)
    finally:
        diagnostics.logger.removeHandler(handler)
        handler.close()
    ended = datetime.now().astimezone().isoformat(timespec='milliseconds')
    log = path.read_text()
    warnings = [line for line in log.splitlines() if 'Slow database lock wait' in line]
    assert len(warnings) == 2
    assert 'phase=live probe: background contention' in warnings[0] and 'background=True' in warnings[0]
    assert 'phase=live probe: foreground contention' in warnings[1] and 'background=False' in warnings[1]
    assert all('yield=0.000s outcome=ok' in line for line in warnings)
    print(f'Live lock log window: start={started} end={ended} path={path}')


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
