"""Worker checkpoints retain their published groups until the worker advances."""
import json
import sqlite3

import pytest

from tests.group_support import publish, recordings
from tests.repos.test_group_retention import drain, enable
from yt_playlist.core import group_preparation_state, group_retention_progress
from yt_playlist.repos import group_queries, group_retention


@pytest.mark.parametrize('durable', [False, True])
def test_previous_worker_checkpoint_keeps_its_exact_groups_until_advanced(file_backed_store, monkeypatch, durable):
    store = file_backed_store
    first = publish(store, recordings([('Pop', 12, .2)]), now=1)
    assert first['groups']
    with store.conn.atomic():
        group_preparation_state.checkpoint(store.conn, first['generation'], 11, 1)
    second = publish(store, recordings([('House', 12, .8)]), now=2, previous=first)
    assert second['generation'] != first['generation']
    before = group_queries.definitions(store.conn, first['generation'], members=True)
    if not durable:
        monkeypatch.setattr(group_retention_progress, 'available', lambda conn: False)
    enable(store)
    drain(store)
    assert group_queries.definitions(store.conn, first['generation'], members=True) == before
    assert json.loads(group_preparation_state.get(store.conn, 'rec_group_shadow_checkpoint')) == {
        'generation': first['generation'], 'serial': 11, 'at': 1}
    assert store.taste_groups.head()['generation'] == second['generation']
    with store.conn.atomic():
        group_preparation_state.checkpoint(store.conn, second['generation'], 12, 2)
    drain(store)
    assert not store.conn.execute('SELECT 1 FROM group_generations WHERE external_key=?',
                                  (first['generation'],)).fetchone()
    assert store.taste_groups.head()['generation'] == second['generation']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_new_checkpoint_invalidates_completed_walk_and_protects_publication(file_backed_store):
    store = file_backed_store
    first = publish(store, [], now=1)
    publish(store, [], now=2, previous=first)
    enable(store)
    for _ in range(100):
        selected = group_retention.plan(store.conn)
        assert selected is not None
        if 'generation' in selected:
            break
        with store.conn.atomic():
            group_retention.apply(store.conn, selected)
    else:
        pytest.fail('No obsolete publication selected')
    with store.conn.atomic():
        group_preparation_state.checkpoint(store.conn, first['generation'], 11, 1)
    with pytest.raises(RuntimeError, match='roots changed'), store.conn.atomic():
        group_retention.apply(store.conn, selected)
    with pytest.raises(sqlite3.IntegrityError, match='checkpoint'), store.conn.atomic():
        store.conn.execute("INSERT INTO group_builds VALUES (?,'cleanup',0,0,NULL,NULL)",
                           (selected['generation'],))
        store.conn.execute('DELETE FROM group_publications WHERE generation_id=?', (selected['generation'],))
    drain(store)
    assert json.loads(group_preparation_state.get(store.conn, 'rec_group_shadow_checkpoint'))['generation'] == first['generation']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
