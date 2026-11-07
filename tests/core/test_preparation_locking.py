"""Idle maintenance cannot touch a writer connection owned by another worker."""
import threading
from contextlib import contextmanager

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core.preparation import (
    embeddings, legacy_journals, policy_history, profiles, vibe_provenance,
)


@pytest.mark.parametrize('installed', [79, 107], indirect=True)
@pytest.mark.parametrize('preparation', [
    policy_history, profiles, embeddings, legacy_journals, vibe_provenance,
], ids=lambda module: module.__name__.rsplit('.', 1)[-1])
def test_idle_maintenance_serializes_every_statement(installed, preparation, monkeypatch):
    store, _ = installed
    attempted = threading.Event()
    statements, shared_statements, errors, results = [], [], [], []
    acquire = store._lock.acquire
    read_view = store.read_view

    def observed_acquire(*args, **kwargs):
        if threading.current_thread() is worker:
            attempted.set()
        return acquire(*args, **kwargs)

    def trace(sql):
        if threading.current_thread() is worker:
            statements.append((sql, store._lock.held_by_current_thread))
            shared_statements.append(sql)

    @contextmanager
    def traced_reader(*args, **kwargs):
        with read_view(*args, **kwargs) as reader:
            reader.conn.set_trace_callback(lambda sql: statements.append(
                (sql, reader._lock._is_owned())))
            try:
                yield reader
            finally:
                reader.conn.set_trace_callback(None)

    def run():
        try:
            results.append(preparation.maintain(store, 1000))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run, daemon=True)
    monkeypatch.setattr(store._lock, 'acquire', observed_acquire)
    monkeypatch.setattr(store, 'read_view', traced_reader)
    store.conn.set_trace_callback(trace)
    try:
        # Do not induce SQLite's uninterruptible mutex/GIL cycle in the test
        # process. The owning worker holds its Python lock without running SQL.
        with store._lock:
            worker.start()
            assert attempted.wait(5), 'Maintenance did not wait for the connection'
            assert not shared_statements, 'Maintenance ran SQL on another worker\'s connection'
    finally:
        worker.join(5)
        assert not worker.is_alive(), 'Maintenance remained blocked after the writer finished'
        store.conn.set_trace_callback(None)

    assert not errors
    assert results == [0]
    assert statements and all(held for _, held in statements)
    assert not store.conn.in_transaction
    store.settings.set_setting('after maintenance', 'accepted')
    assert store.settings.get_setting('after maintenance') == 'accepted'


@pytest.mark.parametrize('installed', [107], indirect=True)
def test_concurrent_sync_enrichment_and_retention_keep_serving_sql_serialized(installed, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import traceback

    from fastapi.testclient import TestClient

    from tests.test_sync_outcomes import Client
    from yt_playlist.core.db_diagnostics import DiagnosticCursor
    from yt_playlist.library import sync
    from yt_playlist.rec import measurement
    from yt_playlist.web.app import create_app

    store, _ = installed
    account = store.accounts.upsert_account('Main', 'bridge', None, True)
    remote = Client()
    assert sync.sync_all(store, {account: remote}, 100)['status'] == 'complete'
    app = create_app(store, lambda: {account: remote}, background_work=False)
    serving = store.conn
    violations = []
    call = DiagnosticCursor._call
    start = threading.Barrier(3)

    def guarded(cursor, stage, target, *args):
        if cursor.connection is serving and not store._lock.held_by_current_thread:
            violations.append(traceback.format_stack(limit=8))
            raise RuntimeError('Shared connection SQL requires writer admission')
        return call(cursor, stage, target, *args)

    def sync_library():
        start.wait(5)
        for now in range(101, 104):
            assert sync.sync_all(store, {account: remote}, now)['status'] == 'complete'

    def retain_history():
        start.wait(5)
        for now in range(101, 104):
            for preparation in (profiles, embeddings, legacy_journals, vibe_provenance):
                preparation.maintain(store, now)
            assert measurement.maintain_storage(store, now)

    def select_enrichment():
        start.wait(5)
        for now in range(101, 104):
            store.enrichment_queue.next_enrich_batch(40, now=now)

    with monkeypatch.context() as guard:
        guard.setattr(DiagnosticCursor, '_call', guarded)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            with ThreadPoolExecutor(max_workers=3) as workers:
                futures = [workers.submit(target) for target in (sync_library, retain_history, select_enrichment)]
                assert client.get('/storage-maintenance/status').status_code == 200
                assert client.get('/playlists').status_code == 200
                for future in futures:
                    future.result(20)
            assert client.get('/storage-maintenance/status').status_code == 200
            assert client.post('/settings/lastfm-key', data={'key': 'after-concurrent-workers'}).status_code == 204
        assert not violations, ''.join(''.join(stack) for stack in violations)
        assert store.settings.get_setting('lastfm_api_key') == 'after-concurrent-workers'
