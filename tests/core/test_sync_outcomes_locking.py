"""Sync progress must wait for a worker already using the shared connection."""
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import sync_state
from yt_playlist.library import sync_outcomes


@pytest.mark.parametrize('installed', [79, 107], indirect=True)
@pytest.mark.parametrize('operation', ['begin', 'finish'])
def test_sync_progress_serializes_all_sql_with_other_workers(installed, operation, monkeypatch):
    store, _ = installed
    revision = sync_outcomes.begin_attempt(store, 100)
    result = {'status': 'complete', 'accounts': {}, 'resources': {}}
    attempted = threading.Event()
    statements, errors = [], []
    acquire = store._lock.acquire

    def observed_acquire(*args, **kwargs):
        if threading.current_thread() is worker:
            attempted.set()
        return acquire(*args, **kwargs)

    def trace(sql):
        if threading.current_thread() is worker:
            statements.append((sql, store._lock.held_by_current_thread))

    def run():
        try:
            if operation == 'begin':
                sync_outcomes.begin_attempt(store, 200)
            else:
                sync_outcomes.finish_attempt(store, result, 200, expected_revision=revision)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run, daemon=True)
    monkeypatch.setattr(store._lock, 'acquire', observed_acquire)
    store.conn.set_trace_callback(trace)
    try:
        # Represent enrichment's bounded transaction without running concurrent
        # SQLite statements: a regression must fail instead of deadlocking pytest.
        with store._lock:
            worker.start()
            assert attempted.wait(5), 'Sync did not reach connection admission'
            assert not statements, 'Sync ran SQL while another worker owned the connection'
    finally:
        worker.join(5)
        assert not worker.is_alive(), 'Sync did not finish after the connection was released'
        store.conn.set_trace_callback(None)

    assert not errors
    assert statements and all(held for _, held in statements)
    assert float(store.settings.get_setting('last_sync_attempt_at')) == (200 if operation == 'begin' else 100)
    if operation == 'finish':
        assert sync_state.state(store) == result
        assert float(store.settings.get_setting('last_sync_at')) == 200
