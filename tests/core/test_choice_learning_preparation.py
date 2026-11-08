"""Saved learning relations prepare independently of their historical cutover."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.store_fixture_support import guarded_numbered_store as guarded_installed, numbered_store as installed
from yt_playlist.core import choice_learning_state as learning, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import choice_learning as preparation
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Learning preparation owns SQL and files below the HTTP lifecycle."""


def finish(conn, size=16):
    for _ in range(100):
        progress = preparation.JOB.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Choice learning fixture did not finish')


def head(conn, surface='choice_interests'):
    return conn.execute(f'SELECT p.* FROM {preparation.CURRENT} h JOIN {preparation.PUBLICATIONS} p '
                        'ON p.id=h.publication_id WHERE h.surface=?', (surface,)).fetchone()


def seed(store):
    store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 100, album_browse_id='release')
    documents = originals()
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,NULL)', documents.items())
    return documents


def originals():
    """Exact lower-layer documents shared by preparation and native consumers."""
    values = {
        'choice_bandit': {'schema': 1, 'rows': [{'mode_id': 99, 'label': 'Exact été', 'estimate': .5}],
                          'parameters': {'open': .25, 'window_d': 30}, 'excluded': {'unseen': 2}, 'expires_at': None},
        'group_choice_bandit': {'generation': 'missing-generation', 'at': None, 'rows': [
            {'group_id': 'old-group', 'version': 'missing-version', 'shared_from': 'missing-parent',
             'estimate': .6, 'menus': ['first-offer', 'second-offer']}]},
        'choice_interests': {'schema': 1, 'at': 10, 'watermark': 'original', 'contexts_total': 2,
            'contexts': [{'format': 'albums', 'option_id': 'release', 'mode_id': None,
                'keys': ['song|artist', 'unknown|artist'], 'feature': [0., .25],
                'at': 8, 'days': 2, 'episodes': 3, 'episode_support': .75},
                {'format': 'mixes', 'feature': None, 'keys': []}],
            'album_track_days': {'release': {'song|artist': 2, 'unknown|artist': 0}, 'missing-release': {}},
            'interest_version': 'effective'},
    }
    return {surface: json.dumps(value, ensure_ascii=False, indent=2) for surface, value in values.items()}


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_prepared_mix_descriptor_survives_installed_learning_projection(installed, monkeypatch):
    from yt_playlist.core import mix_choices
    store, _ = installed
    regular.migrate(store.conn)
    with store.conn:
        descriptor = mix_choices.descriptor(store.conn, {'genres':['House'], 'label':'Stay close'})
        value = {'contexts':[{'format':'mixes','descriptor_id':descriptor,'keys':[],
                             'feature':None,'mode_id':None,'at':10,'days':1}]}
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,10)', ('choice_interests',json.dumps(value)))
    preparation.JOB.install(store.conn)
    finish(store.conn)
    assert store.rec_proposals.get_proposals('choice_interests') == value
    assert store.conn.execute('SELECT descriptor_id FROM prepared_choice_interest_mix_descriptors').fetchone()[0] == descriptor
    assert mix_choices.collect_dictionary(store.conn) == 0
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [79, 92], indirect=True)
def test_exact_originals_and_typed_relations_preserve_installed_authority(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    preparation.JOB.install(conn)
    assert preparation.JOB.step(conn, batch_size=1).processed == 1
    assert head(conn, 'choice_bandit') is None
    regular.migrate(conn)
    finish(conn)
    for surface, raw in original.items():
        assert preparation.read_original(conn, head(conn, surface)) == raw
        assert store.rec_proposals.get_proposals(surface) == json.loads(raw)
    assert [tuple(row) for row in conn.execute('SELECT raw_key,recording_id IS NOT NULL,resolution_reason '
        'FROM prepared_choice_interest_tracks ORDER BY position')] == [
            ('song|artist', 1, None), ('unknown|artist', 0, 'missing_recording')]
    assert [tuple(row) for row in conn.execute('SELECT position,feature_null,album_id IS NOT NULL '
        'FROM prepared_choice_interest_contexts ORDER BY position')] == [(0, 0, 1), (1, 1, 0)]
    assert [tuple(row) for row in conn.execute('SELECT raw_key,days FROM prepared_choice_album_track_days ORDER BY position')] == [
        ('song|artist', 2), ('unknown|artist', 0)]
    assert [tuple(row) for row in conn.execute(f'SELECT field,raw_key,unresolved_reason FROM {preparation.REFERENCES} ORDER BY position')] == [
        ('generation', 'missing-generation', 'missing_generation'),
        ('version', 'missing-version', 'missing_version'), ('shared_from', 'missing-parent', 'missing_version')]
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='choice_learning_publications'").fetchone()
    assert not conn.execute("SELECT 1 FROM group_evidence_owners WHERE external_key LIKE 'choice-learning:%'").fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert files.collect_orphans(conn) == 0


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_prepared_numerical_contract_matches_genuine_frozen_cutover_on_separate_copy(installed, tmp_path):
    store, _ = installed
    original = seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    upgraded = Store(tmp_path / 'frozen-upgrade.db')
    try:
        upgraded.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:93])
        upgraded._bind_repositories()
        for surface, raw in original.items():
            assert upgraded.rec_proposals.get_proposals(surface) == json.loads(raw)
            staged_id = head(store.conn, surface)['id']
            native_id = upgraded.conn.execute('SELECT publication_id FROM choice_learning_heads WHERE surface=?', (surface,)).fetchone()[0]
            for table in preparation.TABLES[1:]:
                def rows(conn, owner, identity):
                    result = [dict(row) for row in conn.execute(f'SELECT * FROM {owner} WHERE publication_id=?', (identity,))]
                    for row in result:
                        row.pop('publication_id')
                    return result
                assert rows(store.conn, 'prepared_' + table, staged_id) == rows(upgraded.conn, table, native_id)
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [92], indirect=True)
@pytest.mark.parametrize('surface,value,owner,expected', [
    ('choice_interests', {'contexts': [{'feature': [number / 1000 for number in range(600)]}]},
     'prepared_choice_interest_features', 600),
    ('group_choice_bandit', {'rows': [{'menus': ['offer-' + str(number) for number in range(600)]}]},
     'prepared_choice_group_opportunities', 600),
])
def test_nested_relations_checkpoint_individual_members(installed, surface, value, owner, expected):
    store, _ = installed
    store.rec_proposals.put_proposals(surface, value, 10)
    preparation.JOB.install(store.conn)
    prior = 0
    for _ in range(10):
        progress = preparation.JOB.step(store.conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long nested relation did not finish')
    assert store.conn.execute(f'SELECT count(*) FROM {owner}').fetchone()[0] == expected
    assert preparation.read_original(store.conn, head(store.conn, surface)) == json.dumps(value)


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_source_and_reference_changes_reconcile_without_pinning_mutable_owners(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    preparation.JOB.install(conn)
    finish(conn)
    previous = head(conn)
    with conn:
        conn.execute("INSERT INTO recordings(identity_key) VALUES ('unknown|artist')")
    assert preparation.JOB.progress(conn).prepared_revision is None
    finish(conn)
    assert conn.execute('SELECT count(*) FROM prepared_choice_interest_tracks WHERE publication_id=? '
        'AND resolution_reason IS NULL', (head(conn)['id'],)).fetchone()[0] == 2
    assert preparation.read_original(conn, previous) == original['choice_interests']
    # Staged observations must not add a foreign key that blocks source reset.
    with conn:
        conn.execute("DELETE FROM recordings WHERE identity_key='unknown|artist'")
        conn.execute("UPDATE rec_proposals SET surface='other',payload='opaque' WHERE surface='choice_bandit'")
        conn.execute("DELETE FROM rec_proposals WHERE surface='choice_interests'")
    finish(conn)
    assert head(conn) is None and head(conn, 'choice_bandit') is None
    assert preparation.read_original(conn, previous) == original['choice_interests']
    regular.migrate(conn)


@pytest.mark.parametrize('guarded_installed', [92], indirect=True)
def test_published_group_aliases_are_observed_without_creating_retention_owners(guarded_installed):
    from tests.group_support import publish, recordings
    store, _ = guarded_installed
    groups = publish(store, recordings([('Jazz', 16, .2)]), now=10)
    group = groups['groups'][0]
    value = {'generation': groups['generation'], 'rows': [
        {'group_id': group['group_id'], 'version': group['version'], 'shared_from': group['version'], 'menus': []}]}
    store.rec_proposals.put_proposals('group_choice_bandit', value, 11)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    assert [tuple(row) for row in store.conn.execute(f'SELECT field,generation_id IS NOT NULL,'
        f'group_version_id IS NOT NULL,unresolved_reason FROM {preparation.REFERENCES} ORDER BY position')] == [
            ('generation', 1, 0, None), ('version', 0, 1, None), ('shared_from', 0, 1, None)]
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE external_key LIKE 'choice-learning:%'").fetchone()


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_file_io_fences_a_concurrent_replacement_without_holding_a_transaction(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    seed(store)
    preparation.JOB.install(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='choice_bandit'")
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert preparation.JOB.step(conn, batch_size=1).input_revision == 1
        assert not conn.execute(f'SELECT 1 FROM {preparation.PUBLICATIONS}').fetchone()
        finish(conn)
        assert preparation.read_original(conn, head(conn, 'choice_bandit')) == '{}'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_interrupted_members_and_disk_failure_preserve_the_committed_prefix(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    store.rec_proposals.put_proposals('choice_interests', {'contexts': [{'feature': [.1] * 600}]}, 10)
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=128)
    copy = preparation.copy
    def interrupted(*args):
        copy(*args)
        raise KeyboardInterrupt('controlled learning interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(preparation.JOB, copy=interrupted).step(conn, batch_size=128)
    assert preparation.JOB.progress(conn) == checkpoint
    assert conn.execute('SELECT count(*) FROM prepared_choice_interest_features').fetchone()[0] == 126
    publish = files.publish
    def failed(*args):
        raise OSError('controlled learning disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled learning disk failure'):
        preparation.JOB.step(conn, batch_size=128)
    assert preparation.JOB.progress(conn).status == 'failed'
    failed_progress = preparation.JOB.progress(conn)
    regular.migrate(conn)
    assert preparation.JOB.progress(conn) == failed_progress
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, retry=True, batch_size=128)
        finish(reopened.conn, size=128)
        assert reopened.conn.execute('SELECT count(*) FROM prepared_choice_interest_features').fetchone()[0] == 600
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_partial_source_replacement_and_interrupted_final_head_preserve_prior_generation(installed):
    store, _ = installed
    conn = store.conn
    store.rec_proposals.put_proposals('choice_interests', {'contexts': [{'feature': [.1] * 600}]}, 10)
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=128)
    prior = conn.execute(f'SELECT * FROM {preparation.PUBLICATIONS}').fetchone()
    store.rec_proposals.put_proposals('choice_interests', {'contexts': [{'feature': None}]}, 11)
    complete = preparation.complete
    def interrupted(*args):
        complete(*args)
        raise KeyboardInterrupt('controlled learning head interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(preparation.JOB, complete=interrupted).step(conn, batch_size=128)
    assert head(conn) is None
    assert preparation.JOB.progress(conn).processed == checkpoint.processed
    finish(conn)
    assert json.loads(preparation.read_original(conn, head(conn))) == {'contexts': [{'feature': None}]}
    assert conn.execute('SELECT count(*) FROM prepared_choice_interest_features WHERE publication_id=?', (prior['id'],)).fetchone()[0] == 126


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_whitespace_in_original_counts_toward_the_byte_budget(installed):
    store, _ = installed
    raw = '{}' + ' ' * learning.MAX_BYTES
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,1)', ('choice_bandit', raw))
    preparation.JOB.install(store.conn)
    with pytest.raises(MigrationError, match='exceeds its preparation byte budget'):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert not store.conn.execute(f'SELECT 1 FROM {preparation.PUBLICATIONS}').fetchone()
    assert store.conn.execute("SELECT length(payload) FROM rec_proposals WHERE surface='choice_bandit'").fetchone()[0] == len(raw)
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [92], indirect=True)
@pytest.mark.parametrize('surface,raw', [
    ('choice_interests', '{"contexts":[{"feature":[NaN]}]}'),
    ('choice_bandit', '{"rows":[{"mode_id":1,"estimate":2}]}'),
    ('choice_interests', '{"parameters":{"open":null}}'),
    ('group_choice_bandit', '{"rows":[{"menus":["repeat","repeat"]}]}'),
])
def test_invalid_numerical_or_ambiguous_relations_fail_without_changing_source(installed, surface, raw):
    store, _ = installed
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,1)', (surface, raw))
    preparation.JOB.install(store.conn)
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == raw
    regular.migrate(store.conn)
