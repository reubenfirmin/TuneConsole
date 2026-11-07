"""Verified checkpoint runtime selection remains compatible with live writers."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.core.test_model_checkpoint_preparation import seed
from tests.core.test_predecessor_writes import installed
from tests.group_support import publish as publish_groups
from yt_playlist.core import content_preparation_state, group_preparation_state, ranking_checkpoints
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import checkpoint_publication as publication, model_checkpoints as preparation
from yt_playlist.core.store import Store
from yt_playlist.rec import group_preparation


OWNERS = {'content-checkpoints': content_preparation_state, 'group-checkpoints': group_preparation_state,
          'candidate-checkpoints': ranking_checkpoints}


def prepare(store, name):
    job = preparation.JOBS[name]
    job.install(store.conn)
    return finish(job, store.conn).prepared_revision


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
@pytest.mark.parametrize('name', tuple(preparation.JOBS))
def test_existing_and_reopened_readers_match_the_historical_runtime(installed, name, tmp_path):
    store, _ = installed
    sources = seed(store)
    expected = Store(tmp_path / 'historical-checkpoints.db')
    other = Store(store._db_path)
    try:
        store.conn.backup(expected.conn)
        runner.migrate(expected.conn, registry=registry.MIGRATIONS[:94])
        other.init_schema(defer_metadata=True)
        revision = prepare(store, name)
        publication.publish(store.conn, name, revision=revision)
        publication.publish(store.conn, name, revision=revision)
        assert publication.selected(other.conn, name) == revision
        job = preparation.JOBS[name]
        served = {}
        for key in preparation.SOURCES[job.name]:
            value = other.settings.get_setting(key)
            served[key] = value
            historical = OWNERS[name].get(expected.conn, key)
            if key in preparation.CONTENT or key in preparation.GROUP[:2]:
                assert json.loads(value) == json.loads(historical)
            else:
                assert value == historical
        assert other.settings.get_setting('rec_vibe_version') == sources['rec_vibe_version']
        assert dict(store.conn.execute(f'SELECT source,raw_value FROM {publication.ORIGINALS} WHERE name=?', (name,))) == {
            key: sources[key] for key in preparation.SOURCES[job.name]}
        regular.validate(other.conn)
    finally:
        other.conn.close()
        expected.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert publication.selected(reopened.conn, name) == revision
        assert {key: reopened.settings.get_setting(key) for key in served} == served
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name,key,raw', [
    ('content-checkpoints', 'rec_preparation_fit', '{"state":"waiting","attempts":3}'),
    ('group-checkpoints', 'rec_group_shadow_progress', '{"phase":"ready","at":140,"reason":null}'),
    ('candidate-checkpoints', 'rec_candidates_expire_at', '000999.50'),
])
def test_old_and_runtime_writes_invalidate_until_explicit_publication(installed, name, key, raw):
    store, _ = installed
    sources = seed(store)
    revision = prepare(store, name)
    publication.publish(store.conn, name, revision=revision)
    with closing(sqlite3.connect(store._db_path)) as old_writer, old_writer:
        old_writer.execute('UPDATE settings SET value=? WHERE key=?', (raw, key))
    assert publication.selected(store.conn, name) is None
    assert store.settings.get_setting(key) == raw
    assert OWNERS[name].get(store.conn, key) == raw
    revision = finish(preparation.JOBS[name], store.conn).prepared_revision
    assert publication.selected(store.conn, name) is None
    publication.publish(store.conn, name, revision=revision)
    value = store.settings.get_setting(key)
    assert json.loads(value) == json.loads(raw) if name != 'candidate-checkpoints' else value == '999.5'
    assert store.conn.execute(f'SELECT raw_value FROM {publication.ORIGINALS} WHERE name=? AND revision=0 AND source=?',
                              (name, key)).fetchone()[0] == sources[key]
    with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
        with store.conn:
            store.conn.execute(f'DELETE FROM {publication.TABLES[key]}')
    store.settings.set_values({key: sources[key]})
    assert publication.selected(store.conn, name) is None
    assert store.settings.get_setting(key) == sources[key]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_direct_group_checkpoint_writes_and_reference_changes_keep_the_proof_coherent(installed):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, 'group-checkpoints', revision=prepare(store, 'group-checkpoints'))
    with pytest.raises(ValueError, match='published generation'):
        with store.conn:
            group_preparation_state.checkpoint(store.conn, 'unknown', 1, 2)
    generation = publish_groups(store, [])['generation']
    assert publication.selected(store.conn, 'group-checkpoints') is None
    publication.publish(store.conn, 'group-checkpoints', revision=finish(preparation.JOBS['group-checkpoints'], store.conn).prepared_revision)
    with store.conn:
        group_preparation_state.checkpoint(store.conn, generation, 20, 200)
    assert publication.selected(store.conn, 'group-checkpoints') is None
    assert json.loads(store.settings.get_setting('rec_group_shadow_checkpoint')) == {
        'generation': generation, 'serial': 20, 'at': 200}
    publication.publish(store.conn, 'group-checkpoints', revision=finish(preparation.JOBS['group-checkpoints'], store.conn).prepared_revision)
    assert json.loads(store.settings.get_setting('rec_group_shadow_checkpoint'))['generation'] == generation


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('reopen', [False, True])
def test_group_worker_can_publish_after_checkpoint_installation(installed, reopen):
    from tests.group_support import recordings
    from tests.test_group_preparation import seed as seed_corpus
    original, _ = installed
    seed(original)
    seed_corpus(original, recordings([('Pop', 12, .2)]))
    publication.publish(original.conn, 'group-checkpoints', revision=prepare(original, 'group-checkpoints'))
    store = Store(original._db_path) if reopen else original
    try:
        if reopen:
            store.init_schema(defer_metadata=True)
        worker = group_preparation.Preparation(store)
        assert worker.step(200)
        head = store.taste_groups.head()
        assert json.loads(store.settings.get_setting('rec_group_shadow_checkpoint'))['generation'] == head['generation']
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        if reopen:
            store.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(preparation.JOBS))
def test_stale_corrupt_and_interrupted_publication_cannot_select_partial_state(installed, name):
    store, _ = installed
    sources = seed(store)
    revision = prepare(store, name)
    job = preparation.JOBS[name]
    key = preparation.SOURCES[job.name][0]
    store.settings.set_setting(key, sources[key] + ' ')
    with pytest.raises(MigrationError, match='reconciled preparation revision'):
        publication.publish(store.conn, name, revision=revision)
    revision = finish(job, store.conn).prepared_revision
    field = next(iter(preparation.columns(key)))
    value = store.conn.execute(f'SELECT {field} FROM {preparation.TABLES[key]}').fetchone()[0]
    corrupt = 'ready' if field == 'phase' else 999 if type(value) is int else 'corrupt'
    with store.conn:
        store.conn.execute(f'UPDATE {preparation.TABLES[key]} SET {field}=?', (corrupt,))
    with pytest.raises(MigrationError, match='verified input'):
        publication.publish(store.conn, name, revision=revision)
    with store.conn:
        store.conn.execute(f'UPDATE {preparation.TABLES[key]} SET {field}=?', (value,))
    class InterruptCommit:
        def __getattr__(self, attribute):
            return getattr(store.conn, attribute)
        def commit(self):
            raise KeyboardInterrupt('controlled checkpoint publication interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(InterruptCommit(), name, revision=revision)
    assert publication.selected(store.conn, name) is None
    assert not store.conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (publication.HEAD,)).fetchone()
    publication.publish(store.conn, name, revision=revision)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(preparation.JOBS))
def test_background_results_reject_same_connection_checkpoint_edits(installed, name):
    store, _ = installed
    sources = seed(store)
    publication.publish(store.conn, name, revision=prepare(store, name))
    key = preparation.SOURCES[preparation.JOBS[name].name][0]
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting(key)
        store.settings.set_setting(key, sources[key])
        assert not snapshot.publish(lambda: pytest.fail('Published stale checkpoint inputs'))


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(preparation.JOBS))
@pytest.mark.parametrize('raw', [None, ''])
def test_empty_null_blank_and_removed_sources_keep_their_native_meaning(installed, name, raw):
    store, _ = installed
    job = preparation.JOBS[name]
    publication.publish(store.conn, name, revision=prepare(store, name))
    for key in preparation.SOURCES[job.name]:
        assert store.settings.get_setting(key, 'missing') == 'missing'
        with store.conn:
            store.conn.execute('INSERT INTO settings VALUES (?,?)', (key, raw))
    publication.publish(store.conn, name, revision=finish(job, store.conn).prepared_revision)
    for key in preparation.SOURCES[job.name]:
        expected = '' if raw == '' and key in ('rec_mode_fit_inputs', 'rec_candidate_preferences') else 'missing'
        assert store.settings.get_setting(key, 'missing') == expected
    for key in preparation.SOURCES[job.name]:
        store.settings.delete_setting(key)
    publication.publish(store.conn, name, revision=finish(job, store.conn).prepared_revision)
    assert all(store.settings.get_setting(key, 'missing') == 'missing' for key in preparation.SOURCES[job.name])


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('target', ['capture', 'immutability'])
def test_admission_requires_capture_and_publication_definitions(installed, target):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, 'candidate-checkpoints', revision=prepare(store, 'candidate-checkpoints'))
    store.conn.execute('BEGIN')
    try:
        trigger = ('prepare_candidate_checkpoints_settings_update' if target == 'capture' else publication.ORIGINALS + '_delete')
        store.conn.execute('DROP TRIGGER ' + trigger)
        with pytest.raises(MigrationError, match='(preparation|publication) object'):
            regular.validate(store.conn)
    finally:
        store.conn.rollback()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_vibes_are_not_captured_and_native_historical_owners_take_precedence(installed, tmp_path):
    store, _ = installed
    seed(store)
    for name in preparation.JOBS:
        publication.publish(store.conn, name, revision=prepare(store, name))
    candidate = preparation.JOBS['candidate-checkpoints'].progress(store.conn)
    store.settings.set_setting('rec_vibe_version', 'still-excluded')
    assert store.settings.get_setting('rec_vibe_version') == 'still-excluded'
    assert preparation.JOBS['candidate-checkpoints'].progress(store.conn) == candidate
    upgraded = Store(tmp_path / 'published-checkpoint-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        assert all(publication.selected(upgraded.conn, name) is None for name in preparation.JOBS)
        assert ranking_checkpoints.get(upgraded.conn, 'rec_vibe_version') == 'still-excluded'
        regular.validate(upgraded.conn)
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_full_frozen_upgrade_after_publication_retains_its_supported_route(installed, tmp_path):
    store, _ = installed
    seed(store)
    for name in preparation.JOBS:
        publication.publish(store.conn, name, revision=prepare(store, name))
    upgraded = Store(tmp_path / 'published-checkpoint-full-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        assert all(publication.selected(upgraded.conn, name) is None for name in preparation.JOBS)
        regular.validate(upgraded.conn)
    finally:
        upgraded.conn.close()
