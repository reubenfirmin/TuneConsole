"""Real runtime publications retain committed history and collect rollback bytes."""
from yt_playlist.core import file_collection
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from yt_playlist.core import catalog, fao, metadata_native, metadata_runtime, metadata_runtime_files as files
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.preparation import managed_files
from tests.core.test_metadata_runtime import runtime_store, entry
from yt_playlist.core.store import Store


def native(store, track, recording, value, run, now):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        metadata_native.record(store.conn, track, 'native', [MetadataObservation('energy', value,
            'recording', 'identity', 'prediction', unit='classifier_probability')],
            run_id=run, now=now, recording_id=recording)


def collect(store, now, ticks=128):
    # A zero-removal turn can mean grace, pinning, or an unrelated domain.
    # Orphans advances position only after exhausting that domain's iterator.
    # Complete one traversal of every domain, retaining the ordinary per-turn
    # entry budget and a failure bound instead of repeating completed scans.
    assert ticks > 0
    completed = set()
    for _ in range(ticks):
        work = getattr(store.retention.database, '_managed_file_orphans', None)
        position = work.position if work is not None else 0
        store.retention.maintain_managed_files(now)
        work = store.retention.database._managed_file_orphans
        if work.position != position:
            completed.add(position)
        if len(completed) == len(work.contracts):
            return frozenset(work.contracts[index][0] for index in completed)
    pytest.fail(f'Managed-file traversal did not finish within {ticks} turns: '
                f'{[str(root) for index, (root, _) in enumerate(work.contracts) if index not in completed]}')


@pytest.fixture(autouse=True)
def close_collectors(runtime_store):
    try:
        yield
    finally:
        managed_files.close(runtime_store)


@pytest.mark.parametrize('kind', ['conflicts', 'native', 'choices', 'unlinked-native'])
def test_rolled_back_and_unlinked_metadata_collect_without_losing_committed_history(runtime_store, kind):
    store = runtime_store
    track, recording = entry(store)
    at = time.time()
    # All committed decision revisions remain required, not just the head.
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', 120)
        catalog.choose_field(store.conn, track, 'bpm', 130)
    native(store, track, recording, .3, 'first', at)
    store.enrichment.upsert_conflict(track, 'genre', [{'provider': 'a', 'value': 'Jazz'}], now=at)
    root = files.directory(store.conn)
    before = set((p for p in file_collection.walk_entries(root) if p is not None))
    with pytest.raises(RuntimeError, match='rollback publication'):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            if kind == 'conflicts':
                # First conflict for a distinct field leaves no current row.
                metadata_runtime.upsert_conflict(store.conn, track, 'bpm',
                    [{'provider': 'a', 'value': 140}], now=at)
            elif kind == 'choices':
                catalog.choose_field(store.conn, track, 'bpm', 150)
            elif kind == 'native':
                metadata_native.record(store.conn, track, 'other-native', [MetadataObservation('energy', .8,
                    'recording', 'other', 'prediction', unit='classifier_probability')],
                    run_id='rolled-back', now=at, recording_id=recording)
            else:
                # Update the actual source before unlinking it below.
                metadata_native.record(store.conn, track, 'native', [MetadataObservation('energy', .9,
                    'recording', 'identity', 'prediction', unit='classifier_probability')],
                    run_id='rolled-back', now=at + 1, recording_id=recording)
            raise RuntimeError('rollback publication')
    orphan, = set((p for p in file_collection.walk_entries(root) if p is not None)) - before
    if kind == 'unlinked-native':
        native(store, track, recording, .6, 'second', at + 2)
        key, revision = store.conn.execute('SELECT source_key,revision FROM rec_musical_sources').fetchone()
        # The new revision reused the prospective pathname. The old first
        # revision becomes orphaned even with no recording left to enumerate.
        orphan = root / files.relative('native', (key,), revision - 1)
        with store.conn:
            store.conn.execute('DELETE FROM rec_musical_links WHERE source_key=?', (key,))
    committed = {str(root / files.relative(kind_, owner, revision)): fao.read(root / files.relative(kind_, owner, revision)).bytes
                 for kind_, owner, revision, required in files.references(store.conn)
                 if required and kind_ != 'conflicts'}
    assert committed and str(orphan) not in committed
    future = at + 31 * 86400
    assert root in collect(store, at)
    assert orphan.exists(), 'Recent unowned documents retain their grace period'
    with store.read_view():
        assert root in collect(store, future)
        assert orphan.exists(), 'A pinned older snapshot fences the whole domain'
    managed_files.close(store.retention.database)
    collect(store, future)
    assert not orphan.exists()
    assert {path: fao.read(path).bytes for path in committed} == committed
    assert all(files.owns_file(store.conn, path) for path in committed)
    # Normal maintenance legitimately replaces an expired conflict's contents.
    # Its new current document must remain owned and readable.
    identity, revision = store.conn.execute('SELECT id,revision FROM metadata_runtime_conflicts').fetchone()
    current = root / files.relative('conflicts', (identity,), revision)
    assert files.owns_file(store.conn, current)
    assert files.read(store.conn, 'conflicts', (identity,), revision) == {'candidates': []}


def test_stale_collector_keeps_replacement_and_incomplete_publication(runtime_store, monkeypatch):
    store = runtime_store
    track, _recording = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', 120)
    root = files.directory(store.conn)
    with pytest.raises(RuntimeError):
        with store.conn:
            catalog.choose_field(store.conn, track, 'bpm', 140)
            raise RuntimeError('rollback')
    orphan = root / files.relative('choices', (1,), 2)
    assert orphan.exists()
    original = fao.delete
    replaced = []
    def replace_before_delete(path, **kwargs):
        if Path(path) == orphan and not replaced:
            fao.write(orphan, b'new publication')
            replaced.append(True)
        return original(path, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(fao, 'delete', replace_before_delete)
        collect(store, time.time() + 31 * 86400)
    assert replaced and fao.read(orphan).bytes == b'new publication'


@pytest.mark.parametrize('rollback', [False, True])
def test_collection_waits_for_real_publication_transaction(runtime_store, rollback):
    store = runtime_store
    track, _recording = entry(store)
    entered, release = threading.Event(), threading.Event()
    def publish():
        other = Store(store._db_path)
        try:
            with other.conn:
                other.conn.execute('BEGIN IMMEDIATE')
                catalog.choose_field(other.conn, track, 'bpm', 140)
                entered.set()
                assert release.wait(15)
                if rollback:
                    raise RuntimeError('controlled publisher rollback')
        finally:
            other.conn.close()
    root = files.directory(store.conn)
    work = managed_files.Orphans(store.conn)
    work.position = next(index for index, (directory, _) in enumerate(work.contracts) if directory == root)
    try:
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(publish)
            try:
                assert entered.wait(5)
                orphan, = list((p for p in file_collection.walk_entries(root) if p is not None))
                # SQL writer admission must fail before any ownership decision.
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    work.step(store, time.time() + 31 * 86400)
                assert orphan.exists()
            finally:
                release.set()
            if rollback:
                with pytest.raises(RuntimeError, match='controlled publisher rollback'):
                    future.result(timeout=10)
            else:
                future.result(timeout=10)
        collect(store, time.time() + 31 * 86400)
        assert orphan.exists() is not rollback
    finally:
        work.close()


def test_orphan_traversal_is_bounded_and_restarts_make_progress(runtime_store):
    store = runtime_store
    track, _recording = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', 120)
    root = files.directory(store.conn)
    orphans = []
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        for revision in range(2, 42):
            orphans.append(files.publish(store.conn, 'choices', (1,), revision, {'prospective': revision}))
    walk = file_collection.walk_entries
    visited = []
    def bounded(root, *args, **kwargs):
        # Count the outer walk only; its recursive directory entries already
        # flow through this generator, including non-file entries.
        for path in walk(root, *args, **kwargs):
            visited.append(path)
            yield path
    future = time.time() + 31 * 86400
    previous = len(orphans)
    for _restart in range(3):
        work = managed_files.Orphans(store.conn)
        work.position = next(index for index, (directory, _) in enumerate(work.contracts) if directory == root)
        work.paths = bounded(root)
        try:
            for _ in range(5):
                visited.clear()
                work.step(store, future, limit=4)
                assert len(visited) <= 4
        finally:
            work.close()
        remaining = sum(path.exists() for path in orphans)
        assert remaining < previous
        previous = remaining
    collect(store, future)
    assert not any(path.exists() for path in orphans)
    assert files.owns_file(store.conn, root / files.relative('choices', (1,), 1))
