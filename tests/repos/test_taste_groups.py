import sqlite3

import pytest

from tests.group_support import publish, recordings
from tests.mode_fixtures import seed_modes
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.rec import group_discovery, group_versions


def test_split_retains_parent_identity_and_exact_past_membership(store):
    tracks = recordings([('House', 30, .1), ('House', 30, .95)])
    first = publish(store, tracks, policy=group_discovery.Policy(max_depth=0))
    old = first['groups'][0]
    second = publish(store, tracks, 2000, first)
    parent = next(group for group in second['groups'] if group['group_id'] == old['group_id'])
    assert not parent['leaf']
    assert parent['version'] != old['version']
    assert store.taste_groups.definitions(first['generation'], members=True)[0]['members'] == old['members']
    children = [group for group in second['groups'] if group['parent_version'] == parent['version']]
    assert len(children) == 2
    assert set(parent['members']) == set().union(*(set(child['members']) for child in children))
    assert any(edge['source'] == old['version'] and edge['target'] == parent['version']
               and edge['relation'] == 'continuation' for edge in store.taste_groups.lineage())


def test_rename_preserves_identity_and_merge_retains_umbrella(store):
    tracks = recordings([('House', 30, .1), ('House', 30, .95)])
    first = publish(store, tracks)
    root = first['groups'][0]
    merged = publish(store, tracks, 2000, first, policy=group_discovery.Policy(max_depth=0))
    assert merged['groups'][0]['group_id'] == root['group_id']
    assert merged['groups'][0]['leaf']
    assert store.taste_groups.definition(root['version'])['name'] == root['name']


def test_legacy_upgrade_preserves_rows_feedback_and_unknown_historical_membership(tmp_path):
    store = Store(tmp_path / 'groups.db')
    migrate(store.conn, registry=MIGRATIONS[:51])
    store._bind_repositories()
    seed_modes(store, 1, 2)
    store.modes.log_impressions(1, [(0, 1), (1, 2)], 10)
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM taste_modes')]
    store.init_schema()
    assert before == [tuple(row) for row in store.conn.execute('SELECT * FROM taste_modes')]
    assert store.conn.execute('SELECT COUNT(*) FROM rec_mode_impressions').fetchone()[0] == 2
    tracks = recordings([('Pop', 12, .2)])
    first = publish(store, tracks)
    legacy = [group for group in first['groups'] if group.get('legacy')]
    assert len(legacy) == 2 and all(not group['membership_known'] for group in legacy)
    assert all(not group['members'] for group in legacy)
    # Repeat publications must not reuse or overwrite a legacy definition version.
    publish(store, tracks, 2000, first)
    publish(store, tracks, 3000, store.taste_groups.head(members=True))
    assert len(store.taste_groups.legacy_versions()) == 2
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    store.conn.close()


def test_failed_publication_rolls_back_every_artifact_and_preserves_head(store):
    tracks = recordings([('Pop', 12, .2)])
    first = publish(store, tracks)
    result = group_discovery.discover(tracks)
    prepared = group_versions.prepare(result, tracks, first, [], now=2000, serial=0,
        input_fingerprint='second', policy=group_discovery.Policy())
    prepared['edges'].append(('broken', 'edge', None, 1))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute('BEGIN')
        with store.conn:
            store.taste_groups.publish(prepared)
    assert store.taste_groups.head()['generation'] == first['generation']
    assert not store.conn.execute('SELECT 1 FROM rec_group_generations WHERE generation=?',
                                   (prepared['generation'],)).fetchone()


def test_requires_outer_transaction(store):
    with pytest.raises(RuntimeError):
        store.taste_groups.publish({})


def test_definition_and_member_history_cannot_be_rewritten(store):
    head = publish(store, recordings([('Pop', 8, .3)]))
    version = head['groups'][0]['version']
    for sql in ('UPDATE rec_group_definitions SET payload=\'{}\' WHERE version=?',
                'DELETE FROM rec_group_members WHERE version=?'):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            with store.conn:
                store.conn.execute(sql, (version,))
