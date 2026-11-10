"""Actual evidence protects exact history; unused preparations release shared content."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3

import pytest

from tests.core.test_compact_groups_migration import historical_groups, apply
from tests.genre_catalog_support import bound_catalog
from tests.repos.test_compact_group_publication import (
    finish,
    third_generation,
)
from tests.store_fixture_support import native_sql_calls, reuse_reporting_reader
from yt_playlist.core import group_evidence, group_storage, schema_inspection
from yt_playlist.core import group_reads as group_queries
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.store import Store
from yt_playlist.repos import compact_group_publication as compact, group_publication, group_retention
from yt_playlist.repos.group_retention_queries import GroupRetentionRepo


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Group retention owns SQL and file lifetimes without HTTP startup."""


@pytest.fixture(scope='module')
def retention_predecessor():
    # Retention consumes real compacted history, including retired v69 tables.
    with pytest.MonkeyPatch.context() as patch, closing(sqlite3.connect(':memory:')) as source:
        bound_catalog(patch, entries=(('house', 'House'),))
        source.execute('PRAGMA foreign_keys=ON')
        runner.migrate(source, registry=registry.MIGRATIONS[:69])
        source.execute('PRAGMA query_only=ON')
        yield source


@pytest.fixture
def database(tmp_path, retention_predecessor, native_sql_calls):
    # Build the populated predecessor and actual conversion once per private
    # consumer, without an intermediate Store, template capture or file copy.
    path = tmp_path / 'groups.db'
    with historical_groups(path, retention_predecessor) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        prepared = third_generation(conn)
        apply(conn)
    store = Store(path)
    with closing(store.conn), schema_inspection.reuse_tables(store.conn):
        store._bind_repositories()
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._snapshot_revisions = Revisions(store.conn)
        assert store.taste_groups.head_generation() == 'second'
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        yield store, prepared


@pytest.fixture
def retention_reader_repositories(monkeypatch):
    bind = Store._bind_repositories

    def bind_retention_reader(reader):
        if not getattr(reader, '_read_only', False):
            return bind(reader)
        # Readers consume SQL facts through the real retention repository.
        # Keep actual private connections, WAL pinning and publication guards;
        # constructing unrelated application controllers adds no coverage here.
        reader.group_retention = GroupRetentionRepo(reader)

    monkeypatch.setattr(Store, '_bind_repositories', bind_retention_reader)


@pytest.fixture(autouse=True)
def retention_io(database, monkeypatch, retention_reader_repositories):
    store, _ = database
    # Keep real WAL transactions, cleanup and explicit snapshot guards. These
    # retention cases do not own power-loss durability or automatic checkpoints.
    store.conn.execute('PRAGMA synchronous=NORMAL')
    store.conn.execute('PRAGMA wal_autocheckpoint=0')
    # These inputs own group and policy documents. Keep both real file leases
    # without opening unrelated application domains for every private snapshot.
    monkeypatch.setattr(store, '_retained_readers', store.taste_groups.files.retained)
    with reuse_reporting_reader(store, monkeypatch):
        yield


def context(generation='first', version='first-leaf'):
    return {'group_generation': generation, 'musical_groups': [{'version': version, 'group_id': 'leaf'}]}


def enable(store):
    # Only controlled tests may assert this until all runtime producers and
    # historical backfill are installed. Migration leaves this fence closed.
    with store.conn:
        store.conn.execute('UPDATE group_retention_state SET roots_complete=1 WHERE id=1')


def drain(store):
    for _ in range(200):
        if not store.taste_groups.cleanup_staged():
            break
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    else:
        pytest.fail('Root-based retention did not finish bounded cleanup')


def test_retention_is_disabled_until_all_actual_evidence_has_explicit_roots(database):
    store, _ = database
    before = '\n'.join(store.conn.iterdump())
    assert group_queries.retention_plan(store.conn) is None
    assert not store.taste_groups.retire_history()
    assert '\n'.join(store.conn.iterdump()) == before
    with pytest.raises(RuntimeError, match='backfill is incomplete'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_retention.apply(store.conn, {'generation': 1})
    with pytest.raises(sqlite3.IntegrityError, match='backfill is incomplete'), store.conn:
        store.conn.execute("INSERT INTO group_lineage_retirements VALUES ('first-leaf','second-leaf')")


def test_no_evidence_retires_old_aliases_without_copying_or_deleting_shared_members(database):
    store, _ = database
    enable(store)
    members = [tuple(row) for row in store.conn.execute('SELECT * FROM group_members')]
    drain(store)
    assert group_queries.head_generation(store.conn) == 'second'
    assert [row[0] for row in store.conn.execute('SELECT external_key FROM group_generations')] == ['second']
    assert group_queries.definition(store.conn, 'first-leaf') is None
    assert group_queries.definition(store.conn, 'second-leaf', generation='second') is not None
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM group_members')] == members
    assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM group_input_manifests').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['offer', 'response', 'comparison', 'prepared', 'pending', 'checkpoint'])
def test_each_actual_evidence_owner_keeps_its_exact_alias_and_transfer_path(database, kind):
    store, _ = database
    enable(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(store.conn, kind, 'actual-reference', context(), 10)
    drain(store)
    assert group_queries.definition(store.conn, 'first-leaf', generation='first') is not None
    assert [row['target'] for row in group_queries.successors(store.conn, 'first-leaf')] == ['second-leaf']
    assert store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 2
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.release(store.conn, kind, 'actual-reference')
    drain(store)
    assert store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 1


def test_new_evidence_invalidates_a_retirement_plan_before_any_deletion(database):
    store, _ = database
    enable(store)
    with store.read_snapshot() as snapshot:
        selected = group_queries.retention_plan(snapshot.store.conn)
        assert selected and 'edges' in selected
        snapshot.release_reader()
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            group_evidence.retain(store.conn, 'pending', 'new-admission', context(), 10)
        assert not snapshot.publish(lambda: group_retention.apply(store.conn, selected))
    assert store.conn.execute('SELECT count(*) FROM group_lineage').fetchone()[0] == 1
    drain(store)
    assert group_queries.definition(store.conn, 'first-leaf') is not None


def test_active_build_pins_its_parent_then_releases_that_reference_after_publication(database):
    store, prepared = database
    enable(store)
    owner = store.taste_groups.stage(prepared)
    second = store.conn.execute("SELECT id FROM group_generations WHERE external_key='second'").fetchone()[0]
    generations, _ = group_queries.retention_closure(store.conn)
    assert second in generations
    assert store.conn.execute('SELECT parent_generation_id FROM group_builds').fetchone()[0] == second
    finish(store, prepared, owner)
    assert not store.conn.execute('SELECT 1 FROM group_builds').fetchone()
    drain(store)
    assert group_queries.head_generation(store.conn) == 'third'
    assert [row[0] for row in store.conn.execute('SELECT external_key FROM group_generations')] == ['third']


def test_unchanged_preparations_reuse_old_aliases_without_an_unbounded_generation_chain(database):
    store, prepared = database
    enable(store)
    parent = 'second'
    for serial in range(3, 11):
        next_prepared = deepcopy(prepared)
        next_prepared.update(generation=f'repeat-{serial}', at=serial, serial=serial, _stage_parent=parent)
        next_prepared['lineage'] = []
        for group in next_prepared['groups']:
            group['version'] = group['version'].replace('third-', 'second-')
            if group['parent_version']:
                group['parent_version'] = group['parent_version'].replace('third-', 'second-')
        owner = store.taste_groups.stage(next_prepared)
        finish(store, next_prepared, owner)
        drain(store)
        assert store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM group_members').fetchone()[0] == 3
        parent = next_prepared['generation']
    assert group_queries.head_generation(store.conn) == 'repeat-10'
    assert group_queries.definition(store.conn, 'second-leaf', generation='repeat-10')['created_at'] == 10


def test_reused_member_content_does_not_keep_an_unrelated_full_input_manifest(database):
    store, prepared = database
    enable(store)
    versions = [row[0] for row in store.conn.execute('SELECT id FROM group_versions ORDER BY id')]
    content = {version: list(group_storage.version_rows(store.conn, version)) for version in versions}
    original_manifest = store.conn.execute('SELECT id FROM group_input_manifests').fetchone()[0]
    with store.conn:
        store.conn.execute("INSERT INTO recordings(id,identity_key) VALUES (3,'unrelated')")
    metadata = json.loads(prepared['payload'])
    metadata['recording_inputs'] = {'one': '1' * 64, 'two': '2' * 64, 'unrelated': '4' * 64}
    prepared['payload'] = json.dumps(metadata)
    owner = store.taste_groups.stage(prepared)
    finish(store, prepared, owner)
    drain(store)
    assert not store.conn.execute('SELECT 1 FROM group_input_manifests WHERE id=?', (original_manifest,)).fetchone()
    assert not store.conn.execute('SELECT 1 FROM unresolved_group_manifest_inputs').fetchone()
    assert store.conn.execute('SELECT count(*) FROM group_input_manifests').fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM group_recording_inputs').fetchone()[0] == 3
    assert {version: list(group_storage.version_rows(store.conn, version)) for version in versions} == content
    assert store.conn.execute('SELECT count(*) FROM group_members WHERE input_id IS NOT NULL').fetchone()[0] == 3
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_lineage_retirement_bounds_mapping_deletions_instead_of_only_edge_count(database, monkeypatch):
    store, prepared = database
    parent = 'second'
    for serial in range(3, 6):
        repeated = deepcopy(prepared)
        repeated.update(generation=f'repeated-edge-{serial}', at=serial, serial=serial, _stage_parent=parent)
        repeated['lineage'] = [('first-leaf', 'second-leaf', 'continuation',
                                '{"old_share":1,"new_share":1,"overlap":1,"compatible":true}')]
        for group in repeated['groups']:
            group['version'] = group['version'].replace('third-', 'second-')
            if group['parent_version']:
                group['parent_version'] = group['parent_version'].replace('third-', 'second-')
        owner = store.taste_groups.stage(repeated)
        finish(store, repeated, owner)
        parent = repeated['generation']
    before = store.conn.execute('SELECT count(*) FROM group_generation_lineage').fetchone()[0]
    assert before == 4
    enable(store)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 1)
    selected = group_queries.retention_plan(store.conn)
    assert len(selected['edges']) == 1
    changes = []
    store.conn.set_trace_callback(changes.append)
    try:
        assert store.taste_groups.retire_history()
    finally:
        store.conn.set_trace_callback(None)
    assert not store.conn.execute('SELECT 1 FROM group_lineage_retirements').fetchone()
    assert store.conn.execute('SELECT count(*) FROM group_generation_lineage').fetchone()[0] == before - 1
    assert store.conn.execute('SELECT count(*) FROM group_lineage').fetchone()[0] == 1
    assert any('DELETE FROM group_generation_lineage WHERE generation_id=' in sql for sql in changes)
    drain(store)
    assert not store.conn.execute('SELECT 1 FROM group_lineage').fetchone()
    assert store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 2
