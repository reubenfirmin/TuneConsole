"""Compiled serving inputs have typed owners and atomic, retained publications."""
import sqlite3

import pytest

from yt_playlist.core import choice_learning_state as owner
from yt_playlist.core.migrations import migrate, registry, v093_choice_learning
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path):
    store = Store(tmp_path / 'learning.db')
    store.conn.execute('PRAGMA journal_mode=WAL')
    migrate(store.conn, registry=registry.MIGRATIONS[:92])
    store._bind_repositories()
    yield store
    store.conn.close()


def upgrade(store):
    migrate(store.conn, registry=registry.MIGRATIONS)
    store.init_schema()


def interests():
    return {'schema': 1, 'at': 10, 'watermark': 'original', 'contexts_total': 1,
            'contexts': [{'format': 'albums', 'option_id': 'release', 'mode_id': None,
                          'keys': ['song|artist', 'unknown|artist'], 'feature': [0., .25],
                          'at': 8, 'days': 2, 'episodes': 3, 'episode_support': .75}],
            'album_track_days': {'release': {'song|artist': 2}}, 'interest_version': 'effective'}


def test_registered_cutover_preserves_values_unknowns_subjects_and_restart(predecessor):
    store = predecessor
    store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 100, album_browse_id='release')
    original = interests()
    store.rec_proposals.put_proposals('choice_interests', original, None)
    store.rec_proposals.put_proposals('choice_bandit', {'rows': [{'mode_id': 99, 'estimate': .5}], 'expires_at': None}, None)
    upgrade(store)
    assert store.rec_proposals.get_proposals('choice_interests') == original
    assert store.rec_proposals.get_proposals('choice_bandit') == {'rows': [{'mode_id': 99, 'estimate': .5}], 'expires_at': None}
    assert [tuple(row) for row in store.conn.execute('SELECT raw_key,recording_id IS NOT NULL,resolution_reason '
            'FROM choice_interest_tracks ORDER BY position')] == [
                ('song|artist', 1, None), ('unknown|artist', 0, 'missing_recording')]
    assert store.conn.execute('SELECT album_id FROM choice_interest_contexts').fetchone()[0] is not None
    assert store.conn.execute('SELECT album_id FROM choice_album_interests').fetchone()[0] is not None
    assert tuple(store.conn.execute('SELECT resolved_mode_id,resolution_reason FROM choice_mode_estimates').fetchone()) == (None, 'missing_mode')
    assert all(row[0] is None for row in store.conn.execute('SELECT built_at FROM choice_learning_publications'))
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('choice_bandit','group_choice_bandit','choice_interests')").fetchone()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        assert reopened.rec_proposals.get_proposals('choice_interests') == original
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


def test_group_learning_retains_exact_published_aliases_and_replacement_releases_only_its_root(predecessor):
    from tests.group_support import publish, recordings
    store = predecessor
    head = publish(store, recordings([('Jazz', 16, .2)]), now=10)
    group = head['groups'][0]
    value = {'policy': 'compiled-statistics', 'generation': head['generation'], 'at': 11,
             'rows': [{'group_id': group['group_id'], 'version': group['version'], 'estimate': .6,
                       'shared_from': None, 'menus': ['opportunity']}], 'excluded': {'unseen': 0}}
    store.rec_proposals.put_proposals('group_choice_bandit', value, 11)
    upgrade(store)
    assert store.rec_proposals.get_proposals('group_choice_bandit') == value
    root = store.conn.execute('SELECT external_key FROM choice_learning_group_roots').fetchone()[0]
    assert store.conn.execute('SELECT external_version FROM group_evidence_versions WHERE external_key=?', (root,)).fetchone()[0] == group['version']
    with pytest.raises(ValueError, match='cannot be established'):
        store.rec_proposals.put_proposals('group_choice_bandit', {**value, 'generation': 'missing'}, 12)
    assert store.rec_proposals.get_proposals('group_choice_bandit') == value
    store.rec_proposals.put_proposals('group_choice_bandit', {**value, 'rows': []}, 13)
    assert not store.conn.execute('SELECT 1 FROM group_evidence_owners WHERE external_key=?', (root,)).fetchone()


def test_interrupted_cutover_leaves_original_rows_and_schema(predecessor):
    predecessor.rec_proposals.put_proposals('choice_interests', interests(), 10)
    before = list(predecessor.conn.iterdump())
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        v093_choice_learning.apply(predecessor.conn, None)
        raise InterruptedError('Before migration commit')
    assert list(predecessor.conn.iterdump()) == before


def test_publication_rollback_and_snapshot_invalidation_follow_typed_inputs(predecessor):
    upgrade(predecessor)
    predecessor.rec_proposals.put_proposals('choice_interests', interests(), 10)
    with predecessor.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals('choice_interests') == interests()
        snapshot.release_reader()
    replacement = {**interests(), 'interest_version': 'new'}
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        owner.write(predecessor.conn, 'choice_interests', replacement, 11)
        raise InterruptedError('Before publication commit')
    assert predecessor.rec_proposals.get_proposals('choice_interests') == interests()
    predecessor.rec_proposals.put_proposals('choice_interests', replacement, 12)
    assert not snapshot.publish(lambda: pytest.fail('Published from stale learning inputs'))
    assert predecessor.conn.execute('SELECT count(*) FROM choice_learning_publications').fetchone()[0] == 1


def test_sql_enforces_surface_subject_and_immutable_published_inputs(predecessor):
    upgrade(predecessor)
    predecessor.rec_proposals.put_proposals('choice_interests', interests(), 10)
    identity = predecessor.conn.execute('SELECT publication_id FROM choice_learning_heads').fetchone()[0]
    commands = [
        ("INSERT INTO rec_proposals VALUES ('choice_interests','{}',1)", ()),
        ('INSERT INTO choice_learning_heads VALUES (?,?)', ('choice_bandit', identity)),
        ('INSERT INTO choice_mode_estimates(publication_id,position,field_mask,resolution_reason) VALUES (?,0,0,?)', (identity, 'missing_mode')),
        ('UPDATE choice_interest_tracks SET recording_id=999999,resolution_reason=NULL', ()),
        ('UPDATE choice_interest_contexts SET days=99', ()),
        ('DELETE FROM choice_interest_features', ()),
        ('UPDATE choice_learning_publications SET at=99', ()),
        ('DELETE FROM choice_learning_publications', ()),
    ]
    for sql, parameters in commands:
        with pytest.raises(sqlite3.IntegrityError), predecessor.conn.atomic():
            predecessor.conn.execute(sql, parameters)
    assert predecessor.rec_proposals.get_proposals('choice_interests') == interests()


@pytest.mark.parametrize('raw', ['{', '{"contexts":[{"feature":[NaN]}]}',
                                 '{"contexts":[{"feature":[1e999]}]}', '{"unexpected":1}',
                                 '{"parameters":{"open":null}}'])
def test_invalid_old_state_keeps_the_original_for_recovery(predecessor, raw):
    with predecessor.conn:
        predecessor.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('choice_interests', raw, 10))
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError):
        migrate(predecessor.conn, registry=registry.MIGRATIONS)
    assert list(predecessor.conn.iterdump()) == before
