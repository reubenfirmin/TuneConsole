"""Finite collection controls retain decisions while installed writers continue."""
from dataclasses import replace
import json

import pytest

from tests.store_fixture_support import numbered_store as installed
from yt_playlist.core import album_tuning_state, model_selection_heads
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import collection_controls as preparation
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Collection-control preparation does not construct an HTTP app."""


def finish(job, conn, size=1):
    for _ in range(20):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Collection controls did not finish their finite fixture')


def seed(store, *, legacy=False):
    run = store.model_selection.start_selection(10, {})
    with store.conn:
        store.conn.executemany('INSERT INTO genre_catalog(key,name,source) VALUES (?,?,?)',
            [('control-test', 'Control Test', 'custom'), ('control-collision', 'control-test', 'custom')])
        store.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES "
                           "('control alias','Control Alias','control-test','custom')")
    values = {'discovery': .3, 'stretch': .4, 'variety': .5, 'recency': .6} if legacy else {'novelty': .3}
    album = json.dumps({'until': 321.5, 'values': values,
        'genres': ['Control Test', 'Control Alias', 'Unknown Control', 'control-test', 'Control Test']}, indent=2)
    sources = {preparation.ALBUM_KEY: album, 'rec_selection_active_run': '00' + str(run),
        'rec_selection_display_run': '999', 'rec_selection_live_run': None, 'rec_selection_restore': ''}
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', sources.items())
    return sources, run


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
@pytest.mark.parametrize('legacy', [False, True])
def test_exact_originals_order_and_unknown_references_survive_without_admission(
        installed, legacy):
    store, _ = installed
    sources, run = seed(store, legacy=legacy)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    assert tuple(store.conn.execute(f'SELECT source_kind,expires_at,novelty,control_style FROM {preparation.ALBUM}').fetchone()) == (
        'controls', 321.5, .3, 'legacy' if legacy else 'novelty')
    assert [tuple(row) for row in store.conn.execute(f'SELECT position,raw_label,genre_key,unresolved_reason '
        f'FROM {preparation.GENRES} ORDER BY position')] == [
        (0, 'Control Test', 'control-test', None), (1, 'Control Alias', 'control-test', None),
        (2, 'Unknown Control', None, 'unknown_genre'), (3, 'control-test', None, 'ambiguous_genre'),
        (4, 'Control Test', 'control-test', None)]
    assert [tuple(row) for row in store.conn.execute(f'SELECT source,source_kind,recorded_run_id,run_id,unresolved_reason '
        f'FROM {preparation.RUNS} ORDER BY source')] == [
        ('rec_selection_active_run', 'recorded', run, run, None),
        ('rec_selection_display_run', 'recorded', 999, None, 'missing_run'),
        ('rec_selection_live_run', 'null', None, None, None), ('rec_selection_restore', 'blank', None, None, None)]
    retained = {}
    for name in ('album_controls', 'model_pointers'):
        retained.update(store.conn.execute(f'SELECT source,raw_value FROM {preparation.originals(name)}'))
    assert retained == sources
    assert dict(store.conn.execute('SELECT key,value FROM settings WHERE key IN (' + ','.join('?' for _ in sources) + ')',
                                   tuple(sources))) == sources
    assert store.conn.execute('SELECT count(*) FROM rec_model_selection_runs').fetchone()[0] == 1
    assert not album_tuning_state.available(store.conn)
    assert not model_selection_heads.available(store.conn)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_frozen_upgrade_matches_supported_controls_and_run_pointers(installed, tmp_path):
    store, _ = installed
    sources, run = seed(store, legacy=True)
    value = json.loads(sources[preparation.ALBUM_KEY])
    value['genres'] = ['Control Test', 'Control Alias', 'Control Test']
    store.settings.set_values({preparation.ALBUM_KEY: json.dumps(value), 'rec_selection_display_run': str(run)})
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'controls-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        assert json.loads(album_tuning_state.get(upgraded.conn, preparation.ALBUM_KEY)) == {
            'until': 321.5, 'values': {'novelty': .3}, 'genres': ['control-test'] * 3}
        assert [model_selection_heads.get(upgraded.conn, key) for key in model_selection_heads.KEYS] == [
            str(run), str(run), '', '']
        assert [tuple(row) for row in upgraded.conn.execute('SELECT position,genre_key FROM album_menu_control_genres ORDER BY position')] == [
            tuple(row) for row in store.conn.execute(f'SELECT position,genre_key FROM {preparation.GENRES} ORDER BY position')]
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_writes_reference_edits_and_removal_reconcile(installed):
    store, _ = installed
    sources, run = seed(store)
    conn = store.conn
    for job in preparation.JOBS.values():
        job.install(conn)
        finish(job, conn)
    store.settings.set_setting('rec_selection_active_run', '999')
    with conn:
        conn.execute('INSERT INTO rec_model_selection_runs(id,started_at,result_json,identity_version) VALUES (999,20,?,2)', ('{}',))
        conn.execute("UPDATE genre_alias SET genre_key='control-collision' WHERE label_key='control alias'")
    for job in preparation.JOBS.values():
        assert job.progress(conn).prepared_revision is None
        finish(job, conn)
    assert conn.execute(f"SELECT genre_key FROM {preparation.GENRES} WHERE position=1").fetchone()[0] == 'control-collision'
    assert conn.execute(f"SELECT run_id FROM {preparation.RUNS} WHERE source='rec_selection_active_run'").fetchone()[0] == 999
    assert conn.execute(f'SELECT count(*) FROM {preparation.originals("model_pointers")} '
        "WHERE source='rec_selection_active_run'").fetchone()[0] == 2
    with conn:
        conn.execute('DELETE FROM rec_model_selection_runs WHERE id=999')
        conn.execute("UPDATE settings SET key='retained-album-controls',value='opaque text' WHERE key=?", (preparation.ALBUM_KEY,))
    for job in preparation.JOBS.values():
        finish(job, conn)
    assert not conn.execute(f'SELECT 1 FROM {preparation.ALBUM}').fetchone()
    assert not conn.execute(f'SELECT 1 FROM {preparation.GENRES}').fetchone()
    assert conn.execute(f"SELECT unresolved_reason FROM {preparation.RUNS} WHERE source='rec_selection_active_run'").fetchone()[0] == 'missing_run'
    assert conn.execute(f'SELECT raw_value FROM {preparation.originals("album_controls")}').fetchone()[0] == sources[preparation.ALBUM_KEY]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interruption_and_explicit_retry_preserve_independent_checkpoints(installed):
    store, _ = installed
    sources, _ = seed(store)
    conn = store.conn
    album, pointers = preparation.JOBS.values()
    for job in (album, pointers):
        job.install(conn)
    checkpoint = pointers.step(conn, batch_size=1)
    def interrupted(*args):
        preparation.pointer(*args)
        raise KeyboardInterrupt('controlled pointer interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(pointers, copy=interrupted).step(conn, batch_size=1)
    assert pointers.progress(conn) == checkpoint
    store.settings.set_setting(preparation.ALBUM_KEY, 'invalid')
    with pytest.raises(ValueError):
        finish(album, conn)
    assert album.progress(conn).status == 'failed'
    assert finish(pointers, conn).status == 'prepared'
    regular.migrate(conn)
    store.settings.set_setting(preparation.ALBUM_KEY, sources[preparation.ALBUM_KEY])
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            album.step(reopened.conn)
        album.step(reopened.conn, retry=True)
        assert finish(album, reopened.conn).status == 'prepared'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw,kind', [(None, 'null'), ('', 'blank')])
def test_null_blank_and_absent_controls_are_distinct(installed, raw, kind):
    store, _ = installed
    job = preparation.JOBS['album-controls']
    with store.conn:
        store.conn.execute('INSERT INTO settings VALUES (?,?)', (preparation.ALBUM_KEY, raw))
    job.install(store.conn)
    finish(job, store.conn)
    assert store.conn.execute(f'SELECT source_kind FROM {preparation.ALBUM}').fetchone()[0] == kind
    store.settings.delete_setting(preparation.ALBUM_KEY)
    finish(job, store.conn)
    assert not store.conn.execute(f'SELECT 1 FROM {preparation.ALBUM}').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name,source,raw', [
    ('album-controls', preparation.ALBUM_KEY, 'null'),
    ('album-controls', preparation.ALBUM_KEY, '{"until":1,"values":{"novelty":true},"genres":[]}'),
    ('album-controls', preparation.ALBUM_KEY, '{"until":1e999,"values":{"novelty":0.2},"genres":[]}'),
    ('album-controls', preparation.ALBUM_KEY, json.dumps({'until': 1, 'values': {'novelty': .2}, 'genres': ['label'] * 101})),
    ('album-controls', preparation.ALBUM_KEY, ' ' * (preparation.MAX_BYTES + 1)),
    ('model-pointers', 'rec_selection_active_run', '0'),
    ('model-pointers', 'rec_selection_active_run', '1.0'),
    ('model-pointers', 'rec_selection_active_run', '9223372036854775808'),
    ('model-pointers', 'rec_selection_active_run', '1' * (preparation.MAX_BYTES + 1)),
])
def test_invalid_sources_fail_durably_without_changing_installed_values(
        installed, name, source, raw):
    store, _ = installed
    store.settings.set_setting(source, raw)
    job = preparation.JOBS[name]
    job.install(store.conn)
    with pytest.raises(ValueError):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.settings.get_setting(source) == raw
    regular.migrate(store.conn)
