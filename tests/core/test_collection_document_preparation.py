"""Saved import reports and playlist documents keep sources usable during preparation."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.prepared_fixture_support import assert_upgrade_phase, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import assert_installed_consumer_input, publication_consumer_reads
from yt_playlist.core import collection_recovery, playlist_recipe_state, policy_documents, recommendation_outputs as files, spotify_job_state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import collection_documents as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture
def disable_periodic_library_sync():
    pass


@pytest.mark.parametrize('installed', [79, 93, 96], indirect=True)
def test_collection_document_input_keeps_admitted_history_and_private_guards(installed):
    assert_installed_consumer_input(*installed)


def finish(job, conn, size=128):
    for _ in range(150):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Collection document preparation did not finish bounded fixture')


def seed(store, count=3):
    account = store.accounts.upsert_account('Import', 'import.json', None, True)
    job = {'status': 'running', 'identity_id': account, 'phase': 'import', 'started_at': 2,
           'finished_at': None, 'identity_label': '東京', 'report_schema': 1, 'dismissed': None,
           'playlists_found': 2, 'track_entries': 0, 'report': {'partial': [1, 2, None]}, 'preview_token': ''}
    raw = json.dumps(job, ensure_ascii=False, indent=2)
    with store.conn:
        store.conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (spotify_job_state.KEY, raw))
    recipe = {'media': 'audio', 'model': None, 'facets': {'genres': ['Unknown original'] * count, 'eras': ['2010']},
              'opaque': {'positions': [2, 2, 1], 'label': '東京'}}
    store.playlist_recipes.set_recipe('PLAYLIST', recipe, 3)
    canvas = 'legacy canvas text: {\n no JSON requirement\n 東京}'
    store.playlist_recipes.set_cluster_canvas('PLAYLIST', canvas, None)
    return job, raw, recipe, canvas


def facets(conn, scope='PLAYLIST'):
    head = preparation.head(conn, 'recipes', scope)
    return conn.execute('SELECT * FROM prepared_playlist_documents_facets WHERE source=? AND preparation_revision=? ORDER BY kind,position',
                        (scope, head['preparation_revision'])).fetchall()


@pytest.mark.parametrize('installed', [79, 93, 96], indirect=True)
def test_sources_exact_known_account_facets_repetitions_and_verbatim_canvas(installed):
    store, version = installed
    job_value, raw, recipe, canvas = seed(store)
    axes = list(map(tuple, store.conn.execute('SELECT * FROM taste_axes'))) if version >= 88 else None
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn, size=2)
    imported = preparation.head(store.conn, 'import', spotify_job_state.KEY)
    assert preparation.read_original(store.conn, 'import', imported) == raw
    assert imported['account_id'] == job_value['identity_id'] and imported['account_present'] == 1
    assert imported['unresolved_account_id'] is None and imported['started_at'] == 2 and imported['finished_at'] is None
    assert imported['field_mask'] == sum(1 << spotify_job_state.FIELDS.index(field) for field in job_value if field in spotify_job_state.FIELDS)
    assert collection_recovery.read(imported['recovery_filename'], imported['recovery_digest'], imported['recovery_bytes'],
        kind='spotify-import', owner=spotify_job_state.KEY) == {key: job_value[key] for key in ('report', 'preview_token')}
    captured = preparation.head(store.conn, 'recipes', 'PLAYLIST')
    assert json.loads(preparation.read_original(store.conn, 'recipes', captured)) == recipe
    assert captured['scope_ref'] == 'PLAYLIST' and captured['scope_resolution'] is None and captured['created_at'] == 3
    assert len(facets(store.conn)) == 4
    assert [(row['kind'], row['position'], row['captured_value']) for row in facets(store.conn)] == [
        ('era', 0, '2010'), ('genre', 0, 'Unknown original'), ('genre', 1, 'Unknown original'), ('genre', 2, 'Unknown original')]
    assert all(row['axis_ref'] is None for row in facets(store.conn))
    assert store.playlist_recipes.get_recipe('PLAYLIST') == recipe
    captured = preparation.head(store.conn, 'canvases', 'PLAYLIST')
    assert captured['created_at'] is None
    assert preparation.read_original(store.conn, 'canvases', captured) == canvas
    assert collection_recovery.read(captured['recovery_filename'], captured['recovery_digest'], captured['recovery_bytes'],
        kind='playlist-canvas', owner='PLAYLIST') == canvas
    if axes is not None:
        assert axes == list(map(tuple, store.conn.execute('SELECT * FROM taste_axes')))
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare_native_comparison(store):
    job_value, raw, recipe, canvas = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    originals = {(name, source): preparation.read_original(store.conn, name, preparation.head(store.conn, name, source))
                 for name, source in (('import', spotify_job_state.KEY), ('recipes', 'PLAYLIST'), ('canvases', 'PLAYLIST'))}
    return job_value, recipe, canvas, originals


@pytest.fixture(scope='module')
def native_inputs(upgraded_custody_inputs):
    def get(version, target, **options):
        return upgraded_custody_inputs(version, target, 'collection-document-native-agreement', prepare_native_comparison, **options)

    get.release = lambda version: upgraded_custody_inputs.release(version, 'collection-document-native-agreement')
    return get


@pytest.mark.parametrize('version', [79, 96])
def test_native_comparison_input_keeps_exact_preparation(native_inputs, version):
    template = native_inputs(version, version)
    assert_upgrade_phase(template, version)
    for (name, source), raw in template.result[-1].items():
        assert preparation.read_original(template.source.conn, name,
            preparation.head(template.source.conn, name, source)) == raw


@pytest.mark.parametrize('version,target,phase', custody_upgrade_cases((79, 96)))
def test_native_upgrade_phase_keeps_exact_preparation(native_inputs, version, target, phase):
    template = native_inputs(version, target, phase=phase)
    assert_upgrade_phase(template, target if phase == 'converted' else custody_predecessor(version, target))
    for (name, source), raw in template.result[-1].items():
        assert preparation.read_original(template.source.conn, name,
            preparation.head(template.source.conn, name, source)) == raw


@pytest.mark.parametrize('version', [79, 96])
def test_native_serving_input_keeps_private_read_guards(native_inputs, version):
    template = native_inputs(version, 107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn


@pytest.mark.parametrize('version', [79, 96])
def test_frozen_upgrade_keeps_native_documents_and_preparation_archives(native_inputs, version, tmp_path):
    prepared = native_inputs(version, version)
    store = prepared.source
    job_value, recipe, canvas, originals = prepared.result
    with native_inputs(version, 107).open(tmp_path / 'collection-documents-upgrade.db') as upgraded:
        assert upgraded.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
        upgraded.conn.execute('PRAGMA wal_autocheckpoint=0')
        assert spotify_job_state.load(upgraded.conn) == {'account_id' if key == 'identity_id' else key: value for key, value in job_value.items()}
        assert playlist_recipe_state.load(upgraded.conn, 'PLAYLIST') == recipe
        assert playlist_recipe_state.load_canvas(upgraded.conn, 'PLAYLIST') == canvas
        native = list(map(tuple, upgraded.conn.execute('SELECT kind,position,captured_value FROM playlist_recipe_facets ORDER BY kind,position')))
        assert native == [(row['kind'], row['position'], row['captured_value']) for row in facets(store.conn)]
        collection_recovery.collect_orphans(upgraded.conn)
        for (name, source), raw in originals.items():
            assert preparation.read_original(upgraded.conn, name, preparation.head(upgraded.conn, name, source)) == raw
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_facets_resume_without_document_reads_and_keep_old_generation_during_replacement(installed):
    store, _ = installed
    _, _, recipe, _ = seed(store, count=1040)
    job = preparation.JOBS['recipes']
    job.install(store.conn)
    job.step(store.conn, batch_size=128)
    def authorizer(action, owner, column, *unused):
        if action == sqlite3.SQLITE_READ and owner == 'rec_recipes' and column == 'recipe':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(authorizer)
    try:
        while isinstance(json.loads(job.progress(store.conn).cursor), dict):
            job.step(store.conn)
    finally:
        store.conn.set_authorizer(None)
    finish(job, store.conn)
    old = preparation.head(store.conn, 'recipes', 'PLAYLIST')
    assert len(facets(store.conn)) == 1041
    store.playlist_recipes.set_recipe('PLAYLIST', {'facets': {'eras': ['2000']}}, None)
    job.step(store.conn, batch_size=1)
    assert preparation.head(store.conn, 'recipes', 'PLAYLIST')['preparation_revision'] == old['preparation_revision']
    finish(job, store.conn)
    assert json.loads(preparation.read_original(store.conn, 'recipes', old)) == recipe
    assert [(row['kind'], row['captured_value']) for row in facets(store.conn)] == [('era', '2000')]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_job_account_and_document_writes_reject_stale_plans(installed, monkeypatch):
    store, _ = installed
    _, _, recipe, _ = seed(store)
    job = preparation.JOBS['recipes']
    job.install(store.conn)
    publish, changed = files.publish, False
    def race(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            store.playlist_recipes.set_recipe('PLAYLIST', {'media': None, 'facets': None}, 7)
        return publish(root, encoded)
    monkeypatch.setattr(files, 'publish', race)
    assert job.step(store.conn).processed == 0
    assert preparation.head(store.conn, 'recipes', 'PLAYLIST') is None
    finish(job, store.conn)
    captured = preparation.head(store.conn, 'recipes', 'PLAYLIST')
    assert captured['created_at'] == 7 and captured['member_count'] == 0
    assert json.loads(preparation.read_original(store.conn, 'recipes', captured)) == {'media': None, 'facets': None}
    imported = preparation.JOBS['import']
    imported.install(store.conn)
    with store.conn:
        store.conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps({'status': 'done', 'identity_id': 999}), spotify_job_state.KEY))
    finish(imported, store.conn)
    assert preparation.head(store.conn, 'import', spotify_job_state.KEY)['unresolved_account_id'] == 999
    with store.conn:
        store.conn.execute("INSERT INTO main.identities(id,label,credential_ref,is_master) VALUES (999,'Arrived','arrived.json',0)")
    assert imported.progress(store.conn).prepared_revision is None
    finish(imported, store.conn)
    assert preparation.head(store.conn, 'import', spotify_job_state.KEY)['account_id'] == 999


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interruption_missing_plan_retry_and_independent_regular_change(installed, monkeypatch):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['recipes']
    job.install(store.conn)
    before = job.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy('recipes', *args)
        raise KeyboardInterrupt('controlled document interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupt).step(store.conn, batch_size=1)
    assert job.progress(store.conn) == before
    Path(store.conn.execute('SELECT plan_filename FROM prepared_playlist_documents_generations').fetchone()[0]).unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn)
    failed = job.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_document_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_document_ddl'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (regular.Change(1, 'independent_document_ddl', install, validate),))
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    independent = preparation.JOBS['canvases']
    independent.install(store.conn)
    finish(independent, store.conn)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_distinct_empty_canvas_missing_import_account_null_fields_and_invalid_source_retention(installed):
    store, _ = installed
    for job in preparation.JOBS.values():
        job.install(store.conn)
    store.playlist_recipes.set_cluster_canvas('EMPTY', '', None)
    store.playlist_recipes.set_recipe('EMPTY', {'media': None, 'facets': None}, None)
    for name in ('recipes', 'canvases'):
        finish(preparation.JOBS[name], store.conn)
    assert preparation.read_original(store.conn, 'canvases', preparation.head(store.conn, 'canvases', 'EMPTY')) == ''
    imported = preparation.JOBS['import']
    finish(imported, store.conn)
    assert preparation.head(store.conn, 'import', spotify_job_state.KEY) is None
    for value in (None, '', 'null', json.dumps({'status': 'done'}), json.dumps({'status': 'done', 'identity_id': None})):
        with store.conn:
            store.conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                               (spotify_job_state.KEY, value))
        finish(imported, store.conn)
        captured = preparation.head(store.conn, 'import', spotify_job_state.KEY)
        assert preparation.read_original(store.conn, 'import', captured) == value
        assert captured['account_present'] == int(value is not None and 'identity_id' in value)
    recipe_job = preparation.JOBS['recipes']
    old = preparation.head(store.conn, 'recipes', 'EMPTY')
    for value in ({'facets': []}, {'facets': {'genres': ['']}}, {'media': 'invalid'}, None):
        raw = json.dumps(value)
        with store.conn:
            store.conn.execute("UPDATE rec_recipes SET recipe=? WHERE playlist_ytm='EMPTY'", (raw,))
        with pytest.raises(ValueError):
            recipe_job.step(store.conn, retry=True)
        assert preparation.head(store.conn, 'recipes', 'EMPTY')['preparation_revision'] == old['preparation_revision']
        assert store.conn.execute("SELECT recipe FROM rec_recipes WHERE playlist_ytm='EMPTY'").fetchone()[0] == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_keys_beyond_fixed_boundary_and_dependency_edits_are_reconciled(installed):
    store, _ = installed
    seed(store)
    with store.conn:
        store.conn.execute("INSERT INTO main.playlist_scopes(ref,provider_id,kind) VALUES ('ZZZ-LATER','ZZZ-LATER','shared')")
    job = preparation.JOBS['recipes']
    job.install(store.conn)
    finish(job, store.conn)
    boundary = store.conn.execute(f'SELECT highwater FROM {job.definition.sources}').fetchone()[0]
    with store.conn:
        store.conn.execute('INSERT INTO main.rec_recipes VALUES (?,?,?)',
                           ('ZZZ-LATER', json.dumps({'facets': {'genres': ['Late unknown']}}), 9))
    finish(job, store.conn)
    assert facets(store.conn, 'ZZZ-LATER')[0]['genre_key'] is None
    with store.conn:
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('late-style','Late Style','custom')")
        store.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES ('late unknown','Late unknown','late-style','custom')")
    assert store.conn.execute(f'SELECT highwater FROM {job.definition.sources}').fetchone()[0] == boundary
    captured = job.progress(store.conn)
    prepare = job.prepare
    def race(conn, row, **kwargs):
        result = prepare(conn, row, **kwargs)
        with sqlite3.connect(store._db_path) as writer:
            writer.execute("UPDATE rec_recipes SET recipe=recipe WHERE playlist_ytm='ZZZ-LATER'")
        return result
    assert replace(job, prepare=race).step(store.conn).processed == captured.processed
    assert store.conn.execute(f'SELECT highwater FROM {job.definition.sources}').fetchone()[0] == boundary
    def fail(*args):
        job.copy(*args)
        raise OSError('controlled rescan failure')
    with pytest.raises(OSError, match='controlled rescan failure'):
        replace(job, copy=fail).step(store.conn)
    assert store.conn.execute(f'SELECT highwater FROM {job.definition.sources}').fetchone()[0] == boundary
    job.step(store.conn, retry=True)
    finish(job, store.conn)
    assert preparation.head(store.conn, 'recipes', 'ZZZ-LATER')['created_at'] == 9
    assert facets(store.conn, 'ZZZ-LATER')[0]['genre_key'] == 'late-style'
    with store.conn:
        store.conn.execute("DELETE FROM rec_recipes WHERE playlist_ytm='PLAYLIST'")
    finish(job, store.conn)
    assert preparation.head(store.conn, 'recipes', 'PLAYLIST') is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_durable_file_failure_requires_retry_and_leaves_other_documents_usable(installed, monkeypatch):
    store, _ = installed
    _, _, recipe, canvas = seed(store)
    recipes, canvases = preparation.JOBS['recipes'], preparation.JOBS['canvases']
    for job in (recipes, canvases):
        job.install(store.conn)
    publish = policy_documents._publish_encoded
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled collection document disk failure')
    monkeypatch.setattr(policy_documents, '_publish_encoded', fail)
    with pytest.raises(OSError, match='disk failure'):
        recipes.step(store.conn)
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    finish(canvases, store.conn)
    assert store.playlist_recipes.get_recipe('PLAYLIST') == recipe
    assert store.playlist_recipes.get_cluster_canvas('PLAYLIST') == canvas
    with pytest.raises(MigrationError, match='retry explicitly'):
        recipes.step(store.conn)
    recipes.step(store.conn, retry=True)
    finish(recipes, store.conn)


@pytest.mark.parametrize('version', [79, 96])
def test_native_sources_finish_with_committed_read_only_inputs(native_inputs, version):
    for source in native_inputs.release(version):
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
