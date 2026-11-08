"""Durable retirement closure survives interruption and changing evidence."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from tests.repos.test_compact_group_publication import (
    bounded_compact_group_template as compact_group_template,
    database,
    finish,
)
from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_group_retention import context, enable, drain
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core import group_documents, group_evidence, group_reads, group_retention_policy, group_retention_progress, recommendation_outputs
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.store import Store
from yt_playlist.repos import compact_group_publication as compact, group_retention, group_retention_walk as walk
from yt_playlist.rec import group_roots


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Retirement walks own SQL and file lifetimes without HTTP startup."""


@pytest.fixture
def walking(database):
    store, prepared = database
    # Walk slices and their real rollback/reopen contracts keep WAL commits;
    # automatic checkpoint scheduling and power-loss durability are separate.
    store.conn.execute('PRAGMA synchronous=NORMAL')
    store.conn.execute('PRAGMA wal_autocheckpoint=0')
    with store.conn:
        group_retention_progress.install(store.conn)
    enable(store)
    return store, prepared


def ready(store):
    for _ in range(1000):
        state = store.conn.execute('SELECT phase FROM group_retention_walk WHERE id=1').fetchone()
        if state and state[0] == 'ready':
            return
        assert store.taste_groups.retire_history()
    pytest.fail('Durable closure never completed')


def test_closure_beyond_old_limit_resumes_after_reopen_with_exact_history(walking, monkeypatch):
    store, prepared = walking
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(store.conn, 'offer', 'historical', context(), 10)
    parent, source = 'second', 'second-leaf'
    # Populate the real long chain below the closure/reopen boundary. Release
    # this reporting reader before walking, reopening or collecting artifacts.
    with reuse_reporting_reader(store, monkeypatch):
        for serial in range(3, 17):
            next_prepared = deepcopy(prepared)
            next_prepared.update(generation=f'chain-{serial}', at=serial, serial=serial, _stage_parent=parent)
            target = f'chain-{serial}-leaf'
            next_prepared['lineage'] = [(source, target, 'continuation',
                '{"old_share":1,"new_share":1,"overlap":1,"compatible":true}')]
            for group in next_prepared['groups']:
                group['version'] = group['version'].replace('third-', f'chain-{serial}-')
                if group['parent_version']:
                    group['parent_version'] = group['parent_version'].replace('third-', f'chain-{serial}-')
            owner = store.taste_groups.stage(next_prepared)
            finish(store, next_prepared, owner)
            parent, source = next_prepared['generation'], target
    expected = group_reads.retention_closure(store.conn)
    assert len(expected[1]) == 15
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM group_members ORDER BY 1,2')]
    monkeypatch.setattr(walk, 'BATCH', 2)
    monkeypatch.setattr(group_retention_policy, 'MAX_REFERENCES', 1)
    for _ in range(18):
        assert store.taste_groups.retire_history()
    progress = tuple(store.conn.execute('SELECT * FROM group_retention_walk').fetchone())
    path = store.conn.execute('PRAGMA database_list').fetchone()[2]
    reopened = Store(path)
    try:
        reopened.conn.execute('PRAGMA synchronous=NORMAL')
        reopened.conn.execute('PRAGMA wal_autocheckpoint=0')
        reopened._bind_repositories()
        reopened._snapshot_revisions = Revisions(reopened.conn)
        assert tuple(reopened.conn.execute('SELECT * FROM group_retention_walk').fetchone()) == progress
        ready(reopened)
        actual = ({row[0] for row in reopened.conn.execute('SELECT generation_id FROM group_retention_generations')},
                  {tuple(row) for row in reopened.conn.execute('SELECT * FROM group_retention_edges')})
        assert actual == expected
        drain(reopened)
        assert [tuple(row) for row in reopened.conn.execute('SELECT * FROM group_members ORDER BY 1,2')] == before
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        reopened.conn.close()


def test_new_evidence_invalidates_completed_closure_and_stale_deletion(walking, monkeypatch):
    store, _ = walking
    monkeypatch.setattr(walk, 'BATCH', 1)
    ready(store)
    with store.read_snapshot() as snapshot:
        selected = group_reads.retention_plan(snapshot.store.conn)
        assert selected and 'edges' in selected
        snapshot.release_reader()
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            group_evidence.retain(store.conn, 'pending', 'new-owner', context(), 10)
        assert not snapshot.publish(lambda: group_retention.apply(store.conn, selected))
    old = store.conn.execute('SELECT revision FROM group_retention_walk').fetchone()[0]
    assert store.conn.execute('SELECT revision FROM group_retention_epoch').fetchone()[0] > old
    assert store.taste_groups.retire_history()
    assert store.conn.execute('SELECT phase FROM group_retention_walk').fetchone()[0] == 'reset'
    ready(store)
    drain(store)
    assert store.conn.execute('SELECT count(*) FROM group_lineage').fetchone()[0] == 1
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.release(store.conn, 'pending', 'new-owner')
    drain(store)
    assert store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 1


def test_cancelled_prefix_rolls_back_all_work_and_retries(walking, monkeypatch):
    store, _ = walking
    assert store.taste_groups.retire_history()
    before = '\n'.join(store.conn.iterdump())
    original = group_retention.check_cancellable_work
    calls = 0

    def interrupt():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WorkClosing('cancel after a prepared write')
        original()

    with monkeypatch.context() as patch:
        patch.setattr(group_retention, 'check_cancellable_work', interrupt)
        with pytest.raises(WorkClosing, match='prepared write'):
            store.taste_groups.retire_history()
    assert '\n'.join(store.conn.iterdump()) == before
    ready(store)


def test_guard_uses_compact_index_and_preserves_alias_evidence(walking):
    store, _ = walking
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(store.conn, 'offer', 'alias-only',
                              {'musical_groups': [{'version': 'first-leaf', 'group_id': 'leaf'}]}, 10)
    first = store.conn.execute("SELECT id FROM group_generations WHERE external_key='first'").fetchone()[0]
    plan = store.conn.execute('EXPLAIN QUERY PLAN SELECT 1 FROM group_version_aliases a '
        'CROSS JOIN group_evidence_versions e INDEXED BY ix_group_evidence_version '
        'ON e.group_version_id=a.group_version_id AND e.external_version=a.external_version '
        'WHERE a.first_generation_id=?', (first,)).fetchall()
    detail = ' '.join(row[3] for row in plan)
    assert 'SEARCH a USING INDEX ix_group_alias_first_generation' in detail
    assert 'SEARCH e USING INDEX ix_group_evidence_version (group_version_id=?)' in detail.replace('COVERING ', '')
    assert 'SCAN e' not in detail
    with pytest.raises(sqlite3.IntegrityError, match='Retained musical group publication'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.conn.execute("INSERT INTO group_builds VALUES (?,'cleanup',0,0,NULL,NULL)", (first,))
        store.conn.execute('DELETE FROM group_publications WHERE generation_id=?', (first,))
    ready(store)
    drain(store)


def test_shared_compact_version_does_not_pin_an_unreferenced_external_alias(walking):
    store, _ = walking
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(store.conn, 'offer', 'current-alias',
                              {'musical_groups': [{'version': 'second-leaf', 'group_id': 'leaf'}]}, 10)
    assert store.conn.execute('SELECT count(DISTINCT group_version_id) FROM group_version_aliases '
        "WHERE external_version IN ('first-leaf','second-leaf')").fetchone()[0] == 1
    drain(store)
    assert not store.conn.execute("SELECT 1 FROM group_generations WHERE external_key='first'").fetchone()
    assert store.conn.execute("SELECT 1 FROM group_version_aliases WHERE external_version='second-leaf'").fetchone()


def test_group_pack_collection_keeps_all_owners_and_pinned_readers(walking):
    store, _ = walking
    root = group_documents.directory(store.conn)
    orphan = next(group_documents.publish_many(root, [('generation', {'orphan': True})]))[0]
    owners = [(table, prefix + '_filename') for table, _, prefix, _ in group_documents.OWNERS]
    collector = recommendation_outputs.OrphanCollector(root, owners=owners)
    with store.read_view():
        progress = collector.step(store.conn)
        assert progress.pending and progress.blocked and progress.files == 0
        assert Path(orphan).exists()
    removed = 0
    while True:
        progress = collector.step(store.conn)
        removed += progress.files
        if not progress.pending:
            break
    assert removed == 1
    assert not Path(orphan).exists()
    for _, _, kind, filename, digest, size, offset in group_documents.references(store.conn):
        assert isinstance(group_documents.read(filename, digest, size, offset, kind), dict)


def test_runtime_group_collector_uses_sql_facts_without_a_transaction(walking, monkeypatch):
    from yt_playlist.core import fao
    store, _ = walking
    root = group_documents.directory(store.conn)
    orphan = next(group_documents.publish_many(root, [('generation', {'orphan': True})]))[0]
    collector = store.group_evidence.output_collector(groups=True)
    statements, deleted = [], []
    remove = fao.delete

    def outside_transaction(path, **kwargs):
        assert not store.conn.in_transaction
        deleted.append(str(path))
        return remove(path, **kwargs)

    monkeypatch.setattr(fao, 'delete', outside_transaction)
    store.conn.set_trace_callback(statements.append)
    try:
        with store.read_view():
            result = store.group_evidence.collect_output_files(collector)
            assert result.blocked and not deleted
        while True:
            result = store.group_evidence.collect_output_files(collector)
            if not result.pending:
                break
    finally:
        collector.close()
        store.conn.set_trace_callback(None)
    assert deleted == [orphan]
    assert not any(statement.strip().upper().startswith('BEGIN') for statement in statements)
    assert group_reads.head(store.conn)['generation'] == 'second'


def test_cleanup_budget_does_not_starve_durable_walk(walking, monkeypatch):
    store, _ = walking

    def blocked(*args, **kwargs):
        raise WorkBudgetExceeded('cleanup used its own turn')

    monkeypatch.setattr(compact, 'cleanup', blocked)
    monkeypatch.setattr(group_roots, 'reap_pending', lambda *args: None)
    result = group_roots.maintain_retention(store)
    assert result['pending'] and result['group_steps'] > 0
    assert store.conn.execute('SELECT 1 FROM group_retention_walk').fetchone()
    group_roots.close_retention(store)


def test_retention_planning_leaves_foreground_writes_available(walking, monkeypatch):
    store, _ = walking
    entered, release = threading.Event(), threading.Event()
    original = walk.advance
    errors = []

    def held(conn, sources):
        entered.set()
        assert release.wait(5)
        return original(conn, sources)

    def retire():
        try:
            store.taste_groups.retire_history()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(walk, 'advance', held)
    thread = threading.Thread(target=retire)
    thread.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        with store._lock, store.conn.atomic():
            store.conn.execute("INSERT INTO settings(key,value) VALUES ('foreground-retention-check','1')")
        assert time.monotonic() - started < .5
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert store.conn.execute("SELECT value FROM settings WHERE key='foreground-retention-check'").fetchone()[0] == '1'
    assert not store.conn.in_transaction


@pytest.mark.parametrize('external', [False, True])
def test_empty_lineage_scan_is_reused_until_an_edge_owner_changes(walking, monkeypatch, external):
    store, _ = walking
    scans = []
    delete = compact._delete

    def observe(conn, table, *args, **kwargs):
        if table == 'group_lineage':
            scans.append(True)
        return delete(conn, table, *args, **kwargs)

    monkeypatch.setattr(compact, '_delete', observe)
    with store.conn.atomic():
        assert not compact._lineage_orphans(store.taste_groups.repository)
    with store.conn.atomic():
        assert not compact._lineage_orphans(store.taste_groups.repository)
    assert len(scans) == 1
    source, target = store.conn.execute('SELECT source_alias,target_alias FROM group_lineage').fetchone()
    conn = sqlite3.connect(store._db_path) if external else store.conn
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO group_lineage_retirements VALUES (?,?)', (source, target))
            conn.execute('DELETE FROM group_generation_lineage WHERE source_alias=? AND target_alias=?', (source, target))
            conn.execute('DELETE FROM group_lineage_retirements')
    finally:
        if external:
            conn.close()
    with store.conn.atomic():
        assert compact._lineage_orphans(store.taste_groups.repository)
    assert len(scans) == 2
    assert not store.conn.execute('SELECT 1 FROM group_lineage').fetchone()
