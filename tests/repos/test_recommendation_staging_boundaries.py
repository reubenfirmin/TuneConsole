"""Independent durable-state observations across bounded staging transactions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.core.test_predecessor_writes import installed
from tests.publication_support import plan, publish
from yt_playlist.core import db_checkpoint, recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.repos import proposal_cache, recommendation_publication as publication


def output_plan(store, key, count):
    return replace(plan(store, key, count=3), outputs=tuple(
        prepared_output(index) for index in range(count)))


def prepared_output(index):
    key = f'chunk-{index:02d}'
    return publication.Output('home', key, outputs.encode('home', key, {'title': f'Output {index}'}))


def manifest(store, generation):
    # Observe only committed state from an independent connection.
    with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        return reader.execute('SELECT chunk_key,filename,content_digest,byte_size FROM prepared_outputs '
                              'WHERE generation_id=? ORDER BY chunk_key', (generation,)).fetchall()


def test_failed_first_candidate_batch_rolls_back_its_new_pool_header(file_backed_store, monkeypatch):
    store = file_backed_store
    prepared = plan(store, 'first-batch', count=130)
    build = store.recommendations.claim(prepared)

    def interrupted(*args, **kwargs):
        raise WorkClosing('first batch interrupted')

    monkeypatch.setattr(pools, 'append', interrupted)
    with pytest.raises(WorkClosing, match='first batch interrupted'):
        store.recommendations.stage(prepared, build)
    assert not store.conn.execute('SELECT 1 FROM recommendation_pools WHERE content_digest=?',
                                  (prepared.slots[0].pool.content_digest,)).fetchone()
    assert not store.conn.execute('SELECT 1 FROM recommendation_build_pools WHERE generation_id=?',
                                  (build.generation_id,)).fetchone()
    assert store.recommendations.head('home') is None
    assert not store.conn.in_transaction


def test_failed_slot_attachment_leaves_verified_prefix_unsealed_until_retry(file_backed_store):
    store = file_backed_store
    previous = publish(store, plan(store, 'previous'))
    prepared = plan(store, 'attachment-failure', count=130)
    build = store.recommendations.claim(prepared)
    store.conn.execute('CREATE TEMP TRIGGER reject_slot BEFORE INSERT ON recommendation_surface_pools '
        "BEGIN SELECT RAISE(ABORT, 'attachment interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='attachment interrupted'):
        store.recommendations.stage(prepared, build)
    pool_id, complete = store.conn.execute('SELECT id,complete FROM recommendation_pools WHERE content_digest=?',
                                           (prepared.slots[0].pool.content_digest,)).fetchone()
    assert complete == 0
    assert store.conn.execute('SELECT count(*) FROM recommendation_candidates WHERE pool_id=?',
                              (pool_id,)).fetchone()[0] == 130
    assert store.conn.execute('SELECT pool_id FROM recommendation_build_pools WHERE generation_id=?',
                              (build.generation_id,)).fetchone()[0] == pool_id
    assert store.recommendations.head('home')['id'] == previous.generation_id
    store.conn.execute('DROP TRIGGER reject_slot')
    store.recommendations.stage(prepared, build)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.publish(build, 12)
    assert store.recommendations.head('home')['id'] == build.generation_id
    assert len(store.recommendations.page('home', 'all', 'wheelhouse', limit=256)) == 130


def test_cancelled_output_batch_keeps_only_committed_prefix_and_reuses_durable_files(file_backed_store, monkeypatch):
    store = file_backed_store
    previous = publish(store, plan(store, 'previous'))
    prepared = output_plan(store, 'interrupted-files', 35)
    build = store.recommendations.claim(prepared)
    real_publish = outputs.publish
    produced = []

    def interrupted(root, encoded, **kwargs):
        assert not store._lock.held_by_current_thread and not store.conn.in_transaction
        if len(produced) == 18:
            raise WorkClosing('file publication interrupted')
        result = real_publish(root, encoded, **kwargs)
        produced.append(result)
        return result

    monkeypatch.setattr(outputs, 'publish', interrupted)
    with pytest.raises(WorkClosing, match='file publication interrupted'):
        store.recommendations.stage(prepared, build)
    committed = manifest(store, build.generation_id)
    assert len(committed) == 16
    assert all(Path(filename).is_file() for filename, _, _ in produced)
    assert store.recommendations.head('home')['id'] == previous.generation_id
    retried = []

    def resume(root, encoded, **kwargs):
        retried.append(encoded)
        return real_publish(root, encoded, **kwargs)

    monkeypatch.setattr(outputs, 'publish', resume)
    store.recommendations.stage(prepared, build)
    assert retried == [output.encoded for output in prepared.outputs[16:]]
    assert manifest(store, build.generation_id)[:16] == committed
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.publish(build, 12)
    for index in range(35):
        assert store.recommendations.output('home', f'chunk-{index:02d}') == {'title': f'Output {index}'}


def test_failed_manifest_transaction_resumes_after_reopen_without_partial_batch(file_backed_store):
    store = file_backed_store
    prepared = output_plan(store, 'interrupted-owner', 35)
    build = store.recommendations.claim(prepared)
    store.conn.execute("CREATE TEMP TRIGGER reject_output BEFORE INSERT ON prepared_outputs "
        "WHEN NEW.chunk_key='chunk-17' BEGIN SELECT RAISE(ABORT, 'owner interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='owner interrupted'):
        store.recommendations.stage(prepared, build)
    committed = manifest(store, build.generation_id)
    assert len(committed) == 16
    assert store.recommendations.head('home') is None
    path = store._db_path
    store.conn.close()
    reopened = Store(path)
    try:
        reopened.init_schema()
        assert manifest(reopened, build.generation_id) == committed
        reclaimed = reopened.recommendations.claim(prepared, owner=build.owner)
        reopened.recommendations.stage(prepared, reclaimed)
        assert manifest(reopened, build.generation_id)[:16] == committed
        assert len(manifest(reopened, build.generation_id)) == 35
        with reopened.conn:
            reopened.conn.execute('BEGIN IMMEDIATE')
            reopened.recommendations.publish(reclaimed, 12)
        assert reopened.recommendations.output('home', 'chunk-17') == {'title': 'Output 17'}
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


def test_cancellation_between_manifest_rows_rolls_back_the_whole_batch(file_backed_store, monkeypatch):
    store = file_backed_store
    prepared = output_plan(store, 'cancel-owner-rows', 17)
    build = store.recommendations.claim(prepared)
    original, calls = publication._output, []

    def interrupted(conn, *args):
        original(conn, *args)
        calls.append(True)
        if len(calls) == 2:
            raise WorkClosing('cancelled between owner rows')

    monkeypatch.setattr(publication, '_output', interrupted)
    with pytest.raises(WorkClosing, match='cancelled between owner rows'):
        store.recommendations.stage(prepared, build)
    assert manifest(store, build.generation_id) == []
    assert not store.conn.in_transaction
    assert store.recommendations.head('home') is None
    monkeypatch.setattr(publication, '_output', original)
    store.recommendations.stage(prepared, build)
    assert len(manifest(store, build.generation_id)) == 17


def test_process_exit_before_manifest_commit_resumes_its_committed_prefix(file_backed_store):
    store = file_backed_store
    previous = publish(store, plan(store, 'previous'))
    prepared = output_plan(store, 'process-exit', 35)
    build = store.recommendations.claim(prepared)
    # The child exits immediately after its eighteenth durable file, before
    # any owner from the second batch is committed or its contexts unwind.
    child = subprocess.run([sys.executable, '-c', '''
import os
import sys
from tests.repos.test_recommendation_staging_boundaries import output_plan
from yt_playlist.core import recommendation_outputs as outputs
from yt_playlist.core.store import Store

store = Store(sys.argv[1])
store.init_schema()
prepared = output_plan(store, 'process-exit', 35)
build = store.recommendations.claim(prepared, owner=sys.argv[2])
original, produced = outputs.publish, []
def exit_after_file(*args, **kwargs):
    result = original(*args, **kwargs)
    produced.append(result)
    if len(produced) == 18:
        os._exit(73)
    return result
outputs.publish = exit_after_file
store.recommendations.stage(prepared, build)
raise AssertionError('Expected abrupt process exit')
''', str(store._db_path), build.owner], capture_output=True, text=True, timeout=40)
    assert child.returncode == 73, child.stderr
    committed = manifest(store, build.generation_id)
    assert len(committed) == 16
    assert store.recommendations.head('home')['id'] == previous.generation_id
    reclaimed = store.recommendations.claim(prepared, owner=build.owner)
    store.recommendations.stage(prepared, reclaimed)
    assert manifest(store, build.generation_id)[:16] == committed
    assert len(manifest(store, build.generation_id)) == 35
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.publish(reclaimed, 12)
    assert store.recommendations.output('home', 'chunk-18') == {'title': 'Output 18'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_file_collector_cannot_retire_a_batch_before_its_manifest_commit(file_backed_store, monkeypatch):
    store = file_backed_store
    prepared = output_plan(store, 'file-custody', 20)
    build = store.recommendations.claim(prepared)
    real_publish, published, attempts = outputs.publish, [], []

    def collect(root):
        collector = outputs.OrphanCollector(root)
        try:
            with closing(sqlite3.connect(store._db_path)) as connection:
                return collector.step(connection)
        finally:
            collector.close()

    with ThreadPoolExecutor(1) as maintenance:
        def observed(root, encoded, **kwargs):
            assert not store._lock.held_by_current_thread and not store.conn.in_transaction
            result = real_publish(root, encoded, **kwargs)
            published.append(result)
            if len(published) == 16:
                assert manifest(store, build.generation_id) == []
                progress = maintenance.submit(collect, root).result(3)
                attempts.append(progress)
                assert progress.blocked and progress.files == 0
                assert all(Path(filename).is_file() for filename, _, _ in published)
            return result

        monkeypatch.setattr(outputs, 'publish', observed)
        store.recommendations.stage(prepared, build)
    assert len(attempts) == 1
    assert len(manifest(store, build.generation_id)) == 20
    assert outputs.collect_orphans(store.conn) == 0


def test_batched_manifests_yield_to_foreground_and_preserve_pinned_last_good(file_backed_store, monkeypatch):
    store = file_backed_store
    previous = publish(store, plan(store, 'previous'))
    prepared = output_plan(store, 'bounded-manifests', 35)
    original, counts = proposal_cache._write, []
    with store.read_view() as pinned, ThreadPoolExecutor(1) as foreground:
        assert pinned.recommendations.head('home')['id'] == previous.generation_id

        @contextmanager
        def observed(repo, **kwargs):
            with original(repo, **kwargs) as conn:
                yield conn
            assert not repo._lock.held_by_current_thread and not conn.in_transaction
            # The claim itself does not yet assign the caller's Build object.
            generation = conn.execute('SELECT id FROM recommendation_generations WHERE external_key=?',
                                      (prepared.external_key,)).fetchone()[0]
            current = len(manifest(store, generation))
            counts.append(current)
            assert pinned.recommendations.head('home')['id'] == previous.generation_id
            foreground.submit(store.settings.set_setting, 'foreground-during-output-stage', 'saved').result(5)

        monkeypatch.setattr(proposal_cache, '_write', observed)
        build = store.recommendations.claim(prepared)
        store.recommendations.stage(prepared, build)
    increments = [right - left for left, right in zip([0] + counts, counts)]
    assert [size for size in increments if size] == [16, 16, 3]
    assert store.settings.get_setting('foreground-during-output-stage') == 'saved'
    assert store.recommendations.head('home')['id'] == previous.generation_id


def test_staging_checkpoint_failure_keeps_durable_success_and_original_policy(file_backed_store, monkeypatch):
    store = file_backed_store
    prepared = output_plan(store, 'checkpoint-unavailable', 17)
    store.conn.execute('PRAGMA wal_autocheckpoint=17')
    synchronous = store.conn.execute('PRAGMA synchronous').fetchone()[0]
    attempted = []

    def unavailable(*args, **kwargs):
        assert not store._lock.held_by_current_thread and not store.conn.in_transaction
        assert store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 17
        assert store.conn.execute('PRAGMA synchronous').fetchone()[0] == synchronous
        attempted.append(args)
        raise sqlite3.OperationalError('controlled unavailable checkpoint')

    checkpoint_sqlite = SimpleNamespace(**vars(sqlite3))
    checkpoint_sqlite.connect = unavailable
    monkeypatch.setattr(db_checkpoint, 'sqlite3', checkpoint_sqlite)
    build = store.recommendations.claim(prepared)
    store.recommendations.stage(prepared, build)
    assert attempted
    assert len(manifest(store, build.generation_id)) == 17
    assert store.conn.execute('SELECT status FROM recommendation_generations WHERE id=?',
                              (build.generation_id,)).fetchone()[0] == 'ready'
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.publish(build, 12)
    assert store.recommendations.output('home', 'chunk-16') == {'title': 'Output 16'}


def test_staging_keeps_installed_layouts_writable_without_changing_migration_history(installed):
    store, _ = installed
    first = publish(store, plan(store, 'last-good'))
    prepared = output_plan(store, 'installed-batches', 17)
    build = store.recommendations.claim(prepared)
    with store.read_snapshot() as pinned:
        assert pinned.store.recommendations.head('home')['id'] == first.generation_id
        store.recommendations.stage(prepared, build)
        assert store.recommendations.head('home')['id'] == first.generation_id
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            store.recommendations.publish(build, 12)
        assert pinned.store.recommendations.head('home')['id'] == first.generation_id
    assert store.recommendations.head('home')['id'] == build.generation_id
    assert store.recommendations.output('home', 'chunk-16') == {'title': 'Output 16'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
