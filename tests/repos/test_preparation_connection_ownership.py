"""Explicit preparation SQL ports share the connection's actual serving owner."""
from concurrent.futures import ThreadPoolExecutor
import threading

from yt_playlist.core.priority import ServingLock
from yt_playlist.repos.workflow_preparation import WorkflowSources


def test_source_queries_wait_for_the_serving_writer(file_backed_store, monkeypatch):
    store = file_backed_store
    store.settings.set_setting('workflow-source', 'committed')
    repository = WorkflowSources(store.conn)
    assert repository._lock is store._lock
    attempted = threading.Event()
    acquire = store._lock.acquire
    def observed(*args, **kwargs):
        attempted.set()
        return acquire(*args, **kwargs)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with store._lock:
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                store.conn.execute("UPDATE settings SET value='draft' WHERE key='workflow-source'")
                with monkeypatch.context() as guard:
                    guard.setattr(store._lock, 'acquire', observed)
                    pending = workers.submit(repository.source, 'workflow-source', 128)
                    assert attempted.wait(5)
                    assert not pending.done()
            finally:
                store.conn.rollback()
        assert bytes(pending.result(timeout=5)['value']) == b'committed'


def test_rebound_connections_keep_the_current_owner(file_backed_store):
    store = file_backed_store
    original = store._lock
    store._lock = ServingLock(store.priority)
    store._bind_repositories()
    assert store._lock is not original
    assert WorkflowSources(store.conn)._lock is store._lock
    with store.read_view() as reader:
        assert WorkflowSources(reader.conn)._lock is reader._lock
