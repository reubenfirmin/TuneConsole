"""Verified control selection stays coherent with continuing installed writers."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.core.test_collection_control_preparation import finish, seed
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import album_tuning_state, model_selection_heads
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import collection_controls as preparation, control_publication as publication
from yt_playlist.core.store import Store


def prepare(store, name):
    job = preparation.JOBS[name]
    job.install(store.conn)
    return finish(job, store.conn).prepared_revision


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
@pytest.mark.parametrize('name', ['album-controls', 'model-pointers'])
def test_publication_changes_runtime_reads_on_existing_and_reopened_connections(installed, name):
    store, _ = installed
    sources, run = seed(store, legacy=True)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        revision = prepare(store, name)
        publication.publish(store.conn, name, revision=revision)
        publication.publish(store.conn, name, revision=revision)
        assert publication.selected(other.conn, name) == revision
        if name == 'album-controls':
            assert json.loads(other.settings.get_setting(preparation.ALBUM_KEY)) == {
                'until': 321.5, 'values': {'novelty': .3},
                'genres': ['control-test', 'control-test', 'Unknown Control', 'control-test', 'control-test']}
        else:
            assert other.settings.get_setting('rec_selection_active_run') == str(run)
            assert other.settings.get_setting('rec_selection_display_run') == '999'
            assert other.settings.get_setting('rec_selection_live_run') == ''
        expected_sources = {key: value for key, value in sources.items()
                            if (key == preparation.ALBUM_KEY) == (name == 'album-controls')}
        served = {key: other.settings.get_setting(key) for key in expected_sources}
        assert dict(store.conn.execute(f'SELECT source,raw_value FROM {publication.ORIGINALS} WHERE name=?', (name,))) == expected_sources
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert publication.selected(reopened.conn, name) == revision
        assert {key: reopened.settings.get_setting(key) for key in served} == served
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', ['album-controls', 'model-pointers'])
def test_old_writes_invalidate_selection_until_new_revision_publishes(installed, name):
    store, _ = installed
    sources, run = seed(store, legacy=True)
    revision = prepare(store, name)
    publication.publish(store.conn, name, revision=revision)
    key = preparation.ALBUM_KEY if name == 'album-controls' else 'rec_selection_active_run'
    raw = json.dumps({'until': 999, 'values': {'novelty': .8}, 'genres': ['Control Alias']}) if name == 'album-controls' else '000999'
    with closing(sqlite3.connect(store._db_path)) as old_writer, old_writer:
        old_writer.execute('UPDATE settings SET value=? WHERE key=?', (raw, key))
    assert publication.selected(store.conn, name) is None
    # Direct helpers also handle selection changing after available() returned.
    owner = album_tuning_state if name == 'album-controls' else model_selection_heads
    assert owner.get(store.conn, key) == raw
    assert store.settings.get_setting(key) == raw
    next_revision = finish(preparation.JOBS[name], store.conn).prepared_revision
    assert publication.selected(store.conn, name) is None
    publication.publish(store.conn, name, revision=next_revision)
    if name == 'album-controls':
        assert json.loads(store.settings.get_setting(key))['genres'] == ['control-test']
    else:
        assert store.settings.get_setting(key) == '999'
    assert store.conn.execute(f'SELECT raw_value FROM {publication.ORIGINALS} WHERE name=? AND revision=? AND source=?',
                              (name, revision, key)).fetchone()[0] == sources[key]
    with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
        with store.conn:
            store.conn.execute(f'DELETE FROM {publication.ORIGINALS} WHERE name=?', (name,))
    # Runtime writes preserve the installed representation and invalidate once.
    store.settings.set_setting(key, sources[key])
    assert publication.selected(store.conn, name) is None
    assert store.settings.get_setting(key) == sources[key]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_reference_updates_invalidate_only_their_own_selection(installed):
    store, _ = installed
    seed(store)
    for name in preparation.JOBS:
        publication.publish(store.conn, name, revision=prepare(store, name))
    with store.conn:
        store.conn.execute("UPDATE genre_alias SET genre_key='control-collision' WHERE label_key='control alias'")
    assert publication.selected(store.conn, 'album-controls') is None
    assert publication.selected(store.conn, 'model-pointers') == 0
    publication.publish(store.conn, 'album-controls', revision=finish(preparation.JOBS['album-controls'], store.conn).prepared_revision)
    assert json.loads(store.settings.get_setting(preparation.ALBUM_KEY))['genres'][1] == 'control-collision'
    with store.conn:
        store.conn.execute('INSERT INTO rec_model_selection_runs(id,started_at,result_json,identity_version) VALUES (999,20,?,2)', ('{}',))
    assert publication.selected(store.conn, 'album-controls') is not None
    assert publication.selected(store.conn, 'model-pointers') is None
    publication.publish(store.conn, 'model-pointers', revision=finish(preparation.JOBS['model-pointers'], store.conn).prepared_revision)
    assert store.conn.execute(f"SELECT run_id FROM {publication.RUNS} WHERE revision=1 AND source='rec_selection_display_run'").fetchone()[0] == 999


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', ['album-controls', 'model-pointers'])
@pytest.mark.parametrize('reopen', [False, True])
def test_background_results_reject_source_and_reference_changes(installed, name, reopen):
    original, _ = installed
    sources, _ = seed(original)
    publication.publish(original.conn, name, revision=prepare(original, name))
    store = Store(original._db_path) if reopen else original
    try:
        if reopen:
            store.init_schema(defer_metadata=True)
        key = preparation.ALBUM_KEY if name == 'album-controls' else 'rec_selection_active_run'
        for mutation in ('reference', 'source'):
            with store.read_snapshot() as snapshot:
                assert snapshot.store.settings.get_setting(key) is not None
                if mutation == 'reference':
                    with store.conn:
                        if name == 'album-controls':
                            store.conn.execute("DELETE FROM genre_alias WHERE label_key='control alias'")
                        else:
                            store.conn.execute('INSERT INTO rec_model_selection_runs(id,started_at,result_json,identity_version) '
                                               "VALUES (999,20,'{}',2)")
                else:
                    store.settings.set_setting(key, sources[key])
                assert not snapshot.publish(lambda: pytest.fail('Published a result using stale controls'))
            publication.publish(store.conn, name, revision=finish(preparation.JOBS[name], store.conn).prepared_revision)
    finally:
        if reopen:
            store.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_inactive_model_selection_does_not_consume_preparation_counters(installed):
    store, _ = installed
    seed(store)
    name = 'model-pointers'
    publication.publish(store.conn, name, revision=prepare(store, name))
    store.settings.set_setting('rec_selection_active_run', '999')
    assert publication.selected(store.conn, name) is None
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('rec_selection_active_run') == '999'
        # This is normal output of the model-selection workflow. It updates
        # reconciliation counters but cannot change the already-inactive head.
        store.model_selection.start_selection(20, {})
        store.settings.set_setting('unrelated-progress', 'continuing')
        assert snapshot.publish(lambda: None)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', ['album-controls', 'model-pointers'])
def test_stale_corrupt_and_interrupted_publication_keeps_the_old_owner(installed, name):
    store, _ = installed
    sources, _ = seed(store)
    revision = prepare(store, name)
    key = preparation.ALBUM_KEY if name == 'album-controls' else 'rec_selection_active_run'
    store.settings.set_setting(key, sources[key] + ' ' if name == 'album-controls' else '999')
    with pytest.raises(MigrationError, match='reconciled preparation revision'):
        publication.publish(store.conn, name, revision=revision)
    revision = finish(preparation.JOBS[name], store.conn).prepared_revision
    store.conn.execute('BEGIN')
    try:
        if name == 'album-controls':
            store.conn.execute(f'UPDATE {preparation.ALBUM} SET novelty=.9')
        else:
            store.conn.execute(f"UPDATE {preparation.RUNS} SET recorded_run_id=1000 WHERE source=?", (key,))
        # The publisher requires an idle connection, so commit the controlled
        # corrupt staging mutation rather than bypassing its transaction guard.
        store.conn.commit()
        with pytest.raises(MigrationError, match='verified input'):
            publication.publish(store.conn, name, revision=revision)
    finally:
        with store.conn:
            if name == 'album-controls':
                store.conn.execute(f'UPDATE {preparation.ALBUM} SET novelty=.3')
            else:
                store.conn.execute(f'UPDATE {preparation.RUNS} SET recorded_run_id=999 WHERE source=?', (key,))
    class InterruptCommit:
        def __getattr__(self, attribute):
            return getattr(store.conn, attribute)
        def commit(self):
            raise KeyboardInterrupt('controlled publication interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(InterruptCommit(), name, revision=revision)
    assert publication.selected(store.conn, name) is None
    assert not store.conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (publication.HEAD,)).fetchone()
    publication.publish(store.conn, name, revision=revision)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', ['album-controls', 'model-pointers'])
def test_empty_selection_can_publish_and_later_source_can_be_removed(installed, name):
    store, _ = installed
    publication.publish(store.conn, name, revision=prepare(store, name))
    key = preparation.ALBUM_KEY if name == 'album-controls' else 'rec_selection_active_run'
    assert store.settings.get_setting(key, 'missing') == 'missing'
    raw = json.dumps({'until': 100, 'values': {'novelty': .2}, 'genres': []}) if name == 'album-controls' else '999'
    store.settings.set_setting(key, raw)
    assert publication.selected(store.conn, name) is None
    publication.publish(store.conn, name, revision=finish(preparation.JOBS[name], store.conn).prepared_revision)
    store.settings.delete_setting(key)
    assert store.settings.get_setting(key, 'missing') == 'missing'
    publication.publish(store.conn, name, revision=finish(preparation.JOBS[name], store.conn).prepared_revision)
    assert store.settings.get_setting(key, 'missing') == 'missing'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_retained_sources_feed_the_explicit_frozen_upgrade(installed, tmp_path):
    store, _ = installed
    sources, run = seed(store, legacy=True)
    value = json.loads(sources[preparation.ALBUM_KEY])
    value['genres'] = ['Control Alias', 'Control Test']
    store.settings.set_values({preparation.ALBUM_KEY: json.dumps(value), 'rec_selection_display_run': str(run)})
    for name in preparation.JOBS:
        publication.publish(store.conn, name, revision=prepare(store, name))
    upgraded = Store(tmp_path / 'published-controls-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        assert all(publication.selected(upgraded.conn, name) is None for name in preparation.JOBS)
        assert json.loads(album_tuning_state.get(upgraded.conn, preparation.ALBUM_KEY)) == {
            'until': 321.5, 'values': {'novelty': .3}, 'genres': ['control-test', 'control-test']}
        assert model_selection_heads.get(upgraded.conn, 'rec_selection_active_run') == str(run)
        regular.validate(upgraded.conn)
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('target', ['capture', 'immutability'])
def test_admission_requires_capture_and_retained_generation_definitions(installed, target):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, 'model-pointers', revision=prepare(store, 'model-pointers'))
    store.conn.execute('BEGIN')
    try:
        name = ('prepare_model_pointers_settings_update' if target == 'capture' else
                publication.ORIGINALS + '_delete')
        store.conn.execute('DROP TRIGGER ' + name)
        with pytest.raises(MigrationError, match='(preparation|publication) object'):
            regular.validate(store.conn)
    finally:
        store.conn.rollback()
