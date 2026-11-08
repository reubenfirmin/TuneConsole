"""Independent finite v94 checkpoint jobs preserve the installed decisions."""
from dataclasses import replace
import json

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.group_support import publish
from tests.store_fixture_support import (
    guarded_numbered_store as guarded_installed,
    numbered_store as installed,
    preparation_ddl_connection,
    reuse_reporting_reader,
)
from yt_playlist.core import content_preparation_state, group_preparation_state, ranking_checkpoints
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import collection_controls, model_checkpoints as preparation
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Checkpoint preparation keeps SQL and files without HTTP startup."""


def seed(store):
    sources = {
        'rec_preparation_basis': json.dumps({'serial': 42, 'rows': 123, 'at': 100.25,
            'reason': 'new inputs', 'estimated_bytes': 2048, 'dimensions': 16, 'input_fingerprint': 'basis-token',
            'modes': [4, 3, .4], 'phase_bytes': {'encoding': 123, 'publication': 45}}, indent=2),
        'rec_preparation_full': json.dumps({'content': 4, 'evidence': 8, 'rows': 123, 'at': None,
            'config': ['svd', '16', None, '0004', '4', '3', '.4']}),
        'rec_preparation_fit': json.dumps({'state': 'deferred', 'reason': 'memory', 'attempted_at': 101,
            'attempts': 2, 'since': 100, 'next_attempt_at': 150, 'memory_bytes': 1024,
            'estimated_bytes': 2048, 'rows': 123, 'dimensions': 16, 'max_rows': 99,
            'phase_bytes': {'clustering': 345}}),
        'rec_group_shadow_progress': json.dumps({'phase': 'deferred', 'at': 110, 'reason': 'new generation'}),
        'rec_group_shadow_checkpoint': json.dumps({'generation': 'not-admitted', 'serial': 99, 'at': 109}),
        'rec_mode_fit_inputs': ' exact group fingerprint ',
        'rec_candidate_preferences': ' exact preferences ',
        'rec_candidates_expire_at': ' 00123.50 ',
        'rec_vibe_version': 'excluded-vibe-state',
    }
    store.settings.set_values(sources)
    return sources


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_typed_fields_exact_originals_and_unknown_generation_without_admission(installed, preparation_ddl_connection, monkeypatch):
    store, _ = installed
    sources = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    basis = store.conn.execute('SELECT * FROM prepared_content_basis_checkpoint').fetchone()
    assert (basis['serial'], basis['rows'], basis['has_modes'], basis['mode_count'], basis['mode_min_members'],
            basis['mode_match_threshold'], basis['has_phases'], basis['bytes_encoding'], basis['bytes_clustering'],
            basis['bytes_publication']) == (42, 123, 1, 4, 3, .4, 1, 123, None, 45)
    full = store.conn.execute('SELECT * FROM prepared_content_full_checkpoint').fetchone()
    assert full['at'] is None and full['restore_run'] == '0004' and full['has_config'] == 1
    fit = store.conn.execute('SELECT * FROM prepared_content_fit_checkpoint').fetchone()
    assert (fit['state'], fit['attempts'], fit['bytes_clustering'], fit['max_rows']) == ('deferred', 2, 345, 99)
    group = store.conn.execute('SELECT * FROM prepared_group_generation_checkpoint').fetchone()
    assert (group['recorded_generation'], group['generation_id'], group['unresolved_reason'], group['input_serial'],
            group['checkpoint_at']) == ('not-admitted', None, 'missing_generation', 99, 109)
    assert store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0] == 0
    assert store.conn.execute('SELECT expires_at FROM prepared_candidate_expiry_checkpoint').fetchone()[0] == 123.5
    actual = {}
    for name in preparation.SOURCES:
        actual.update(store.conn.execute('SELECT source,raw_value FROM ' + collection_controls.originals(name)))
    assert actual == {key: value for key, value in sources.items() if key != 'rec_vibe_version'}
    with reuse_reporting_reader(store, monkeypatch):
        assert all(store.settings.get_setting(key) == value for key, value in sources.items())
    assert not content_preparation_state.available(store.conn)
    assert not group_preparation_state.available(store.conn)
    assert not ranking_checkpoints.available(store.conn)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(preparation_ddl_connection)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_frozen_upgrade_matches_checkpoint_domain_fields(installed, tmp_path):
    store, _ = installed
    sources = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'checkpoint-upgrade.db')
    try:
        upgraded.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        for key in preparation.CONTENT:
            row = dict(store.conn.execute('SELECT * FROM ' + preparation.TABLES[key]).fetchone())
            native = dict(upgraded.conn.execute('SELECT * FROM ' + content_preparation_state.KEYS[key]).fetchone())
            assert {field: row[field] for field in native if field != 'singleton'} == {
                field: value for field, value in native.items() if field != 'singleton'}
        assert json.loads(group_preparation_state.get(upgraded.conn, 'rec_group_shadow_checkpoint')) == json.loads(sources['rec_group_shadow_checkpoint'])
        assert ranking_checkpoints.get(upgraded.conn, 'rec_candidates_expire_at') == '123.5'
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('guarded_installed', [93], indirect=True)
def test_continuing_writes_renames_and_generation_arrival_reconcile(guarded_installed):
    store, _ = guarded_installed
    sources = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    # A real compact group publication creates its own generation/files. Staging
    # only resolves the exact existing generation and never creates one itself.
    generation = publish(store, [])['generation']
    store.settings.set_values({'rec_group_shadow_checkpoint': json.dumps({'generation': generation, 'serial': 100, 'at': 111}),
                               'rec_preparation_basis': '{}', 'rec_candidates_expire_at': '456'})
    for job in preparation.JOBS.values():
        assert job.progress(store.conn).prepared_revision is None
        finish(job, store.conn)
    assert store.conn.execute('SELECT generation_id FROM prepared_group_generation_checkpoint').fetchone()[0] == store.conn.execute(
        'SELECT id FROM group_generations WHERE external_key=?', (generation,)).fetchone()[0]
    assert store.conn.execute('SELECT has_modes,has_phases,serial FROM prepared_content_basis_checkpoint').fetchone()[:] == (0, 0, None)
    with store.conn:
        store.conn.execute("UPDATE settings SET key='retained-content-basis' WHERE key='rec_preparation_basis'")
    finish(preparation.JOBS['content-checkpoints'], store.conn)
    assert not store.conn.execute('SELECT 1 FROM prepared_content_basis_checkpoint').fetchone()
    assert store.conn.execute("SELECT raw_value FROM prepared_content_checkpoints_originals WHERE source='rec_preparation_basis' "
                              'AND raw_value=?', (sources['rec_preparation_basis'],)).fetchone()
    previous = preparation.JOBS['candidate-checkpoints'].progress(store.conn)
    store.settings.set_setting('rec_vibe_version', 'still-excluded')
    assert preparation.JOBS['candidate-checkpoints'].progress(store.conn) == previous


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_batch_failure_retry_and_independent_ddl(installed, preparation_ddl_connection):
    store, _ = installed
    sources = seed(store)
    content = preparation.JOBS['content-checkpoints']
    for job in preparation.JOBS.values():
        job.install(store.conn)
    checkpoint = content.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled checkpoint interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(content, copy=interrupt).step(store.conn, batch_size=1)
    assert content.progress(store.conn) == checkpoint
    store.settings.set_setting('rec_preparation_fit', '{"state":"unknown"}')
    with pytest.raises(ValueError):
        finish(content, store.conn)
    assert content.progress(store.conn).status == 'failed'
    assert finish(preparation.JOBS['candidate-checkpoints'], store.conn).status == 'prepared'
    assert finish(preparation.JOBS['group-checkpoints'], store.conn).status == 'prepared'
    regular.migrate(preparation_ddl_connection)
    store.settings.set_setting('rec_preparation_fit', sources['rec_preparation_fit'])
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            content.step(reopened.conn)
        content.step(reopened.conn, retry=True)
        assert finish(content, reopened.conn).status == 'prepared'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(preparation.JOBS))
@pytest.mark.parametrize('raw,kind', [(None, 'null'), ('', 'blank')])
def test_null_blank_then_removed_sources_keep_their_exact_original(installed, name, raw, kind):
    store, _ = installed
    job = preparation.JOBS[name]
    key = preparation.SOURCES[job.name][0]
    with store.conn:
        store.conn.execute('INSERT INTO settings VALUES (?,?)', (key, raw))
    job.install(store.conn)
    finish(job, store.conn)
    assert store.conn.execute('SELECT source_kind FROM ' + preparation.TABLES[key]).fetchone()[0] == kind
    store.settings.delete_setting(key)
    finish(job, store.conn)
    assert not store.conn.execute('SELECT 1 FROM ' + preparation.TABLES[key]).fetchone()
    assert store.conn.execute('SELECT raw_value FROM ' + collection_controls.originals(job.name)).fetchone()[0] == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name,key,raw', [
    ('content-checkpoints', 'rec_preparation_basis', '{"serial":true}'),
    ('content-checkpoints', 'rec_preparation_basis', '{"modes":[4,3,2]}'),
    ('content-checkpoints', 'rec_preparation_basis', '{"phase_bytes":{"unknown":2}}'),
    ('content-checkpoints', 'rec_preparation_full', '{"config":["one"]}'),
    ('content-checkpoints', 'rec_preparation_fit', '{"at":1}'),
    ('content-checkpoints', 'rec_preparation_fit', '{"memory_bytes":9223372036854775808}'),
    ('group-checkpoints', 'rec_group_shadow_progress', '{"phase":"ready"}'),
    ('group-checkpoints', 'rec_group_shadow_checkpoint', '{"generation":"missing","serial":3}'),
    ('candidate-checkpoints', 'rec_candidates_expire_at', 'NaN'),
    ('candidate-checkpoints', 'rec_candidate_preferences', 'x' * (preparation.MAX_BYTES + 1)),
])
def test_invalid_or_oversized_inputs_fail_durably_without_changing_settings(installed, preparation_ddl_connection, name, key, raw):
    store, _ = installed
    store.settings.set_setting(key, raw)
    job = preparation.JOBS[name]
    job.install(store.conn)
    with pytest.raises(ValueError):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.settings.get_setting(key) == raw
    regular.migrate(preparation_ddl_connection)
