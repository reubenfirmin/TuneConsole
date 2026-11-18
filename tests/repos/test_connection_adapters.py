"""Preparation adapters cannot enter a serving thread's SQL transaction."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest

from tests.store_fixture_support import file_consumer_store
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.repos.collection_recovery import CollectionRecovery
from yt_playlist.repos.embedding_file_references import EmbeddingSources
from yt_playlist.repos.profile_file_references import ProfileSources
from yt_playlist.repos.workflow_preparation import WorkflowSources


def operation(conn, name):
    if name.startswith('collection'):
        repository = CollectionRecovery(conn)
        return (lambda: list(repository.references())) if name == 'collection-references' else lambda: repository.pinned
    if name == 'workflow':
        repository = WorkflowSources(conn)
        return lambda: tuple(repository.source('adapter-test', 100))
    if name == 'embedding':
        repository = EmbeddingSources(conn)
        return lambda: repository.page('recording', 1)
    repository = ProfileSources(conn)
    return lambda: repository.page('recording', '', 1)


def observe_waiter(monkeypatch, store):
    owner, attempted = get_ident(), Event()
    acquire = store._lock.acquire

    def observed(*args, **kwargs):
        if get_ident() != owner:
            attempted.set()
        return acquire(*args, **kwargs)

    monkeypatch.setattr(store._lock, 'acquire', observed)
    return attempted


@pytest.mark.parametrize('name', ['collection-state', 'collection-references', 'workflow', 'embedding', 'profile'])
def test_adapter_waits_for_the_serving_transaction_to_rollback(store, monkeypatch, name):
    store.settings.set_setting('adapter-test', 'before')
    with atomic_write(store):
        query = operation(store.conn, name)
    expected = query()
    attempted = observe_waiter(monkeypatch, store)
    with ThreadPoolExecutor(1) as workers:
        with pytest.raises(RuntimeError, match='abort owner'):
            with atomic_write(store):
                store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='adapter-test'")
                pending = workers.submit(query)
                assert attempted.wait(5), 'Adapter did not acquire the connection owner lock'
                assert not pending.done(), 'Adapter entered another thread\'s transaction'
                raise RuntimeError('abort owner')
        assert pending.result(5) == expected
    assert store.settings.get_setting('adapter-test') == 'before'


def test_restore_holds_the_owner_lock_through_rollback(store, monkeypatch):
    store.settings.set_setting('adapter-test', 'before')
    recovery, sources = CollectionRecovery(store.conn), WorkflowSources(store.conn)
    attempted = observe_waiter(monkeypatch, store)
    with ThreadPoolExecutor(1) as workers:
        with pytest.raises(RuntimeError, match='abort restore'):
            with recovery.restoring():
                store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='adapter-test'")
                pending = workers.submit(sources.source, 'adapter-test', 100)
                assert attempted.wait(5)
                assert not pending.done()
                assert recovery.pinned
                raise RuntimeError('abort restore')
        assert tuple(pending.result(5)) == ('adapter-test', b'before')
    assert not recovery.pinned


def test_reader_adapter_uses_its_own_connection_while_the_writer_is_busy(file_consumer_store):
    store = file_consumer_store
    store.settings.set_setting('adapter-test', 'committed')
    with store.read_view() as reader, ThreadPoolExecutor(1) as workers:
        sources = WorkflowSources(reader.conn)
        with atomic_write(store):
            store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='adapter-test'")
            assert tuple(workers.submit(sources.source, 'adapter-test', 100).result(5)) == ('adapter-test', b'committed')
