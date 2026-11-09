"""Scheduled reclamation keeps progressing without holding recovery open."""
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import threading

import pytest

from tests.repos.test_typed_recommendation_publication import (
    recommendation_predecessor, recommendations, plan, publish,
)
from tests.test_mode_refresh_worker import scheduled
from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.rec import choice_evidence, group_roots, rec_worker
from yt_playlist.rec.worker_queue import Job
from yt_playlist.controllers.group_retention import GroupRetention
from yt_playlist.repos import recommendation_publication as publication


def orphan(store):
    prepared = plan(store)
    pool = pools.prepare('abandoned', prepared.policy_version, None, 'f' * 64, 'card', [pools.Candidate(1, .5)])
    with store.conn.atomic():
        identity = publication._create_pool(store.conn, pool)
        pools.append(store.conn, identity, pool.candidates, 0)
        store.conn.execute('UPDATE recommendation_pools SET complete=1 WHERE id=?', (identity,))
    return identity


def test_generation_backlog_cannot_starve_orphan_pools(recommendations, monkeypatch):
    store = recommendations
    first = publish(store, plan(store))
    publish(store, plan(store, 'second'))
    abandoned = orphan(store)
    # Pass the collector budgets explicitly: function defaults were bound when
    # the module loaded, so replacing CLEANUP_ROWS cannot constrain this turn.
    monkeypatch.setattr(publication, 'cleanup_generations',
                        partial(publication.cleanup_generations, row_limit=1))
    monkeypatch.setattr(publication, 'cleanup_pools',
                        partial(publication.cleanup_pools, row_limit=1))
    before = list(map(tuple, store.conn.execute('SELECT * FROM recommendation_candidates WHERE pool_id<>?', (abandoned,))))
    assert store.recommendations.cleanup()
    assert store.conn.execute('SELECT status FROM recommendation_generations WHERE id=?',
                              (first.generation_id,)).fetchone()[0] == 'retiring'
    assert not store.conn.execute('SELECT 1 FROM recommendation_candidates WHERE pool_id=?', (abandoned,)).fetchone()
    assert list(map(tuple, store.conn.execute('SELECT * FROM recommendation_candidates WHERE pool_id<>?', (abandoned,)))) == before
    assert store.recommendations.output('home', 'framing') == {'title': 'second'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_interrupted_pool_batch_rolls_back_its_rows_and_fence(recommendations, monkeypatch):
    store = recommendations
    identity = orphan(store)
    before = list(store.conn.iterdump())
    delete = publication._delete

    def interrupt(*args, **kwargs):
        result = delete(*args, **kwargs)
        if args[1] == 'recommendation_candidates':
            raise WorkClosing('interrupted after candidate deletion')
        return result

    monkeypatch.setattr(publication, '_delete', interrupt)
    with pytest.raises(WorkClosing):
        publication.cleanup_pools(store.recommendations.repository)
    assert list(store.conn.iterdump()) == before
    monkeypatch.setattr(publication, '_delete', delete)
    assert publication.cleanup_pools(store.recommendations.repository).pools == 1
    assert not store.conn.execute('SELECT 1 FROM recommendation_pools WHERE id=?', (identity,)).fetchone()


def test_file_batches_resume_after_a_retained_reader_and_keep_build_outputs(recommendations):
    store = recommendations
    publish(store, plan(store))
    build = store.recommendations.claim(plan(store, 'building'))
    store.recommendations.stage(plan(store, 'building'), build)
    kept = {Path(row[0]) for row in store.conn.execute('SELECT filename FROM prepared_outputs')}
    stale = [Path(outputs.publish(outputs.directory(store.conn), outputs.encode('orphan', str(i), i))[0])
             for i in range(3)]
    sizes = sum(path.stat().st_size for path in stale)
    collector = outputs.OrphanCollector(outputs.directory(store.conn))
    try:
        with store.read_view():
            progress = collector.step(store.conn, limit=1)
            assert progress.blocked and progress.pending
            assert all(path.exists() for path in [*stale, *kept])
        removed = byte_size = 0
        for _ in range(10):
            progress = collector.step(store.conn, limit=1)
            assert progress.checked <= 1
            removed += progress.files
            byte_size += progress.byte_size
            if not progress.pending:
                break
        else:
            pytest.fail('File sweep did not finish')
        assert removed == 3 and byte_size == sizes
        assert all(path.exists() for path in kept)
        assert not any(path.exists() for path in stale)
        assert store.recommendations.output('home', 'framing') == {'title': 'one'}
    finally:
        collector.close()


def test_group_budget_cannot_starve_recommendations_or_file_collection(recommendations, monkeypatch):
    store = recommendations
    publish(store, plan(store))
    publish(store, plan(store, 'second'))
    identity = orphan(store)
    stale = Path(outputs.publish(outputs.directory(store.conn), outputs.encode('orphan', 'garbage', 0))[0])
    directory=outputs.directory
    def owned_directory(conn):
        assert conn is not store.conn or store._lock.held_by_current_thread
        return directory(conn)
    monkeypatch.setattr(outputs,'directory',owned_directory)

    def blocked(*args):
        raise WorkBudgetExceeded('group closure is not finished')

    monkeypatch.setattr(GroupRetention, 'step', blocked)
    result = group_roots.maintain_retention(store)
    assert result['pending'] and result['generations'] == 1 and result['pools'] == 1
    assert result['output_files'] == 2 and result['output_bytes'] > 0
    assert not stale.exists()
    assert not store.conn.execute('SELECT 1 FROM recommendation_pools WHERE id=?', (identity,)).fetchone()
    assert store.recommendations.output('home', 'framing') == {'title': 'second'}
    assert not store.activity_paused
    assert store.settings.get_setting(choice_evidence.STORAGE_PENDING) != '1'


def test_interrupted_file_batch_syncs_its_prefix_and_resumes_accounting(recommendations, monkeypatch):
    store = recommendations
    root = outputs.directory(store.conn)
    parent = root / 'aa'
    parent.mkdir(parents=True, exist_ok=True)
    stale = [parent / f'{index:064x}.json' for index in range(3)]
    for path in stale:
        path.write_bytes(b'unowned output')
    synced = []
    sync = outputs._sync_directory

    def observe(directory):
        sync(directory)
        synced.append(directory)

    def stop_after_first_unlink():
        if any(not path.exists() for path in stale):
            raise WorkClosing('after the first unlink')

    collector = outputs.OrphanCollector(root)
    monkeypatch.setattr(outputs, '_sync_directory', observe)
    with monkeypatch.context() as interrupted:
        interrupted.setattr(outputs, 'check_cancellable_work', stop_after_first_unlink)
        with pytest.raises(WorkClosing):
            collector.step(store.conn)
    assert synced == [parent]
    assert sum(path.exists() for path in stale) == 2
    progress = collector.step(store.conn)
    assert progress.files == 3 and progress.byte_size == 3 * len(b'unowned output')
    assert not progress.pending and synced == [parent, parent]
    assert not any(path.exists() for path in stale)


def test_interrupted_file_writer_reservation_rolls_back_and_can_retry(recommendations):
    store = recommendations
    root = outputs.directory(store.conn)
    stale = Path(outputs.publish(root, outputs.encode('orphan', 'cancelled-reservation', 0))[0])
    collector = outputs.OrphanCollector(root)
    interrupted = False

    def cancel():
        nonlocal interrupted
        if store.conn.in_transaction and not interrupted:
            interrupted = True
            return 1
        return 0

    try:
        store.conn.set_progress_handler(cancel, 1)
        with pytest.raises(sqlite3.OperationalError, match='interrupt'):
            collector.step(store.conn)
        assert interrupted and not store.conn.in_transaction
        assert stale.exists()
        store.conn.set_progress_handler(None, 0)
        assert collector.step(store.conn).files == 1
        assert not stale.exists()
        store.settings.set_setting('after-retention-cancellation', 'available')
    finally:
        store.conn.set_progress_handler(None, 0)
        collector.close()


def test_sql_turns_checkpoint_committed_prefixes_after_releasing_the_serving_lock(
        recommendations, monkeypatch):
    from yt_playlist.core import db_checkpoint
    from yt_playlist.repos import proposal_cache

    store = recommendations
    publish(store, plan(store))
    publish(store, plan(store, 'second'))
    abandoned = orphan(store)
    monkeypatch.setattr(publication, 'cleanup_generations',
                        partial(publication.cleanup_generations, row_limit=1))
    monkeypatch.setattr(publication, 'cleanup_pools',
                        partial(publication.cleanup_pools, row_limit=1))
    checkpoints = []

    def checkpoint(path):
        assert path == store._db_path
        assert not store.conn.in_transaction and not store._lock.held_by_current_thread
        checkpoints.append(True)

    # A checkpoint for every row would defeat small retirement transactions.
    monkeypatch.setattr(proposal_cache, 'checkpoint_wal',
                        lambda *args: pytest.fail('Checkpointed an individual retirement prefix'))
    monkeypatch.setattr(db_checkpoint, 'checkpoint_wal', checkpoint)
    monkeypatch.setattr(GroupRetention, 'step', lambda *args: False)
    monkeypatch.setattr(group_roots, 'reap_pending', lambda *args: None)
    from yt_playlist.repos import compact_group_publication
    monkeypatch.setattr(compact_group_publication, 'cleanup', lambda *args, **kwargs: False)
    threshold = store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0]
    progress = group_roots.maintain_retention(store)
    assert progress['generations'] == 1 and progress['pools'] == 1
    assert len(checkpoints) == 2
    assert store.conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == threshold
    assert not store.conn.execute('SELECT 1 FROM recommendation_pools WHERE id=?', (abandoned,)).fetchone()


def test_worker_retries_retirement_promptly_without_entering_storage_recovery(scheduled, monkeypatch):
    worker, targets = scheduled
    calls, now = [], [100]
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(choice_evidence, 'maintain_storage', lambda *args: pytest.fail('Entered activity recovery'))

    def maintain(store):
        assert not store.activity_paused
        calls.append(True)
        return {'pending': len(calls) == 1}

    monkeypatch.setattr(group_roots, 'maintain_retention', maintain)
    worker._poll_retention()
    worker._poll_retention()
    assert len(targets) == 1 and not worker.jobs.visible
    targets.pop()()
    assert worker.jobs.retry[Job.RETENTION].retry_at == 105
    now[0] = 104
    worker._poll_retention()
    assert not targets
    now[0] = 105
    worker._poll_retention()
    targets.pop()()
    # Healthy owners keep the original idle deadline while this owner retries.
    assert len(calls) == 2 and worker.jobs.retry[Job.RETENTION].retry_at == 400
    assert not worker.busy and not worker.jobs


def test_retirement_does_not_reserve_foreground_capture_admission(recommendations, monkeypatch):
    store = recommendations
    head = publish(store, plan(store))
    entered, release = threading.Event(), threading.Event()
    cleanup = publication.cleanup_generations

    def held(repo, **kwargs):
        result = cleanup(repo, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(publication, 'cleanup_generations', held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(group_roots.maintain_retention, store)
        try:
            assert entered.wait(5)
            generation, close = group_roots.pending_head(store, ['home'], 20)
            assert generation == head.generation_id and close is not None
            close()
            assert not store.activity_paused
        finally:
            release.set()
        future.result(timeout=5)
