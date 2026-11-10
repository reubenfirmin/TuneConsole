import sqlite3

import pytest

from tests.group_support import publish, recordings
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


def test_failed_publication_rolls_back_every_artifact_and_preserves_head(store):
    tracks = recordings([('Pop', 12, .2)])
    first = publish(store, tracks)
    result = group_discovery.discover(tracks)
    prepared = group_versions.prepare(result, tracks, first, [], now=2000, serial=0,
        input_fingerprint='second', policy=group_discovery.Policy())
    prepared['edges'].append(('broken', 'edge', None, 1))
    with pytest.raises(ValueError, match='scores must be finite'):
        store.taste_groups.publish(prepared)
    assert store.taste_groups.head()['generation'] == first['generation']
    assert not store.conn.execute('SELECT 1 FROM group_generations WHERE external_key=?',
                                   (prepared['generation'],)).fetchone()


def test_publication_rejects_an_enclosing_transaction(store):
    with store.conn.atomic(), pytest.raises(RuntimeError, match='idle serving connection'):
        store.taste_groups.publish({})


def test_definition_and_member_history_cannot_be_rewritten(store):
    tracks = recordings([('Pop', 8, .3)])
    with store.conn:
        store.conn.execute('INSERT INTO recordings(identity_key) VALUES (?)', (tracks[0].key,))
    head = publish(store, tracks)
    version = head['groups'][0]['version']
    version = store.conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',
                                 (version,)).fetchone()[0]
    for sql in ('UPDATE group_versions SET content_digest=\'changed\' WHERE id=?',
                'DELETE FROM group_members WHERE group_version_id=?',
                'DELETE FROM unresolved_group_members WHERE group_version_id=?'):
        with pytest.raises(sqlite3.IntegrityError, match='immutable|sealed'):
            with store.conn:
                store.conn.execute(sql, (version,))
