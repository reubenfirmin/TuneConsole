"""Actual evidence protects exact history; unused preparations release shared content."""
from copy import deepcopy
import json
import sqlite3

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database, finish
from yt_playlist.core import group_evidence, group_storage
from yt_playlist.repos import compact_group_publication as compact, group_publication, group_queries, group_retention


def context(generation='first', version='first-leaf'):
    return {'group_generation': generation, 'musical_groups': [{'version': version, 'group_id': 'leaf'}]}


def enable(store):
    # Only controlled tests may assert this until all runtime producers and
    # historical backfill are installed. Migration leaves this fence closed.
    with store.conn:
        store.conn.execute('UPDATE group_retention_state SET roots_complete=1 WHERE id=1')


def drain(store):
    for _ in range(200):
        if not compact.cleanup(store.taste_groups) and not group_retention.step(store.taste_groups):
            break
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    else:
        pytest.fail('Root-based retention did not finish bounded cleanup')


def test_retention_is_disabled_until_all_actual_evidence_has_explicit_roots(database):
    store, _ = database
    before = '\n'.join(store.conn.iterdump())
    assert group_retention.plan(store.conn) is None
    assert not group_retention.step(store.taste_groups)
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
        selected = group_retention.plan(snapshot.store.conn)
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
    owner = compact.stage(store.taste_groups, prepared)
    second = store.conn.execute("SELECT id FROM group_generations WHERE external_key='second'").fetchone()[0]
    generations, _ = group_retention.closure(store.conn)
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
        owner = compact.stage(store.taste_groups, next_prepared)
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
    owner = compact.stage(store.taste_groups, prepared)
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
        owner = compact.stage(store.taste_groups, repeated)
        finish(store, repeated, owner)
        parent = repeated['generation']
    before = store.conn.execute('SELECT count(*) FROM group_generation_lineage').fetchone()[0]
    assert before == 4
    enable(store)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 1)
    selected = group_retention.plan(store.conn)
    assert len(selected['edges']) == 1
    changes = []
    store.conn.set_trace_callback(changes.append)
    try:
        assert group_retention.step(store.taste_groups)
    finally:
        store.conn.set_trace_callback(None)
    assert not store.conn.execute('SELECT 1 FROM group_lineage_retirements').fetchone()
    assert store.conn.execute('SELECT count(*) FROM group_generation_lineage').fetchone()[0] == before - 1
    assert store.conn.execute('SELECT count(*) FROM group_lineage').fetchone()[0] == 1
    assert any('DELETE FROM group_generation_lineage WHERE generation_id=' in sql for sql in changes)
    drain(store)
    assert not store.conn.execute('SELECT 1 FROM group_lineage').fetchone()
    assert store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 2
