"""Retain saved Radio identity, ordering and historical report bytes during staging."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.store_fixture_support import numbered_store as installed, preparation_ddl_connection, publication_consumer_reads
from yt_playlist.core import recommendation_outputs as files, recommendation_setting_outputs as outputs
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import saved_outputs as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Saved-output preparation needs SQL and files without HTTP startup."""


def seed(store):
    store.tracks.upsert_track('present-video', 'Song', 'Artist', None, 100)
    store.rec_ledger.record_serving_policy('saved-policy', {'algorithm': 'seed'}, 1)
    run = store.model_selection.start_selection(1, {'kind': 'product'})
    sources = {'radio_seed_offer': json.dumps({'menu_id': 'original-menu', 'comparison': {'run_id': run},
        'tracks': [{'video_id': 'present-video', 'pool_policy': 'saved-policy'},
                   {'video_id': 'missing-video', 'pool_policy': 'missing-policy'}, {'video_id': 'present-video'}]}, indent=2),
        'rec_autotune_result': ' {"ran_at": 12.5, "winner": {"method": "svd", "dim": 48}, "note": "東京"} '}
    store.settings.set_values(sources)
    return sources, run


def head(conn, source):
    generations, current = preparation.tables(source)
    return conn.execute(f'SELECT g.* FROM {current} h JOIN {generations} g USING(source,preparation_revision)').fetchone()


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
def test_exact_files_ordered_references_and_old_readers_remain_usable(installed, preparation_ddl_connection):
    store, _ = installed
    conn = preparation_ddl_connection
    sources, run = seed(store)
    for name, job in preparation.JOBS.items():
        job.install(conn)
        finish(job, conn)
        source = preparation.SOURCES[name]
        assert preparation.read_original(head(conn, source)) == sources[source]
        assert store.settings.get_setting(source) == sources[source]
    assert not outputs.available(conn)
    assert tuple(head(conn, 'radio_seed_offer')[field] for field in ('menu_id', 'comparison_run_id', 'run_ref', 'run_resolution')) == (
        'original-menu', run, run, None)
    assert head(conn, 'rec_autotune_result')['observed_at'] == 12.5
    assert [(row['position'], row['video_id'], row['resolution_reason']) for row in conn.execute(
        f'SELECT * FROM {preparation.SUBJECTS} ORDER BY position')] == [
        (0, 'present-video', None), (1, 'missing-video', 'missing_entry'), (2, 'present-video', None)]
    assert list(map(tuple, conn.execute(f'SELECT raw_version,policy_version,resolution_reason FROM {preparation.POLICIES} ORDER BY raw_version'))) == [
        ('missing-policy', None, 'missing_policy'), ('saved-policy', 'saved-policy', None)]
    assert conn.execute('SELECT count(*) FROM rec_model_selection_runs').fetchone()[0] == 1
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [79, 94], indirect=True)
def test_frozen_upgrade_preserves_original_outputs_and_matches_reference_custody(installed, tmp_path):
    store, _ = installed
    sources, _ = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'saved-outputs-upgrade.db')
    try:
        upgraded.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        for source, raw in sources.items():
            assert outputs.get(upgraded.conn, source) == raw
            assert preparation.read_original(head(upgraded.conn, source)) == raw
        prepared = head(store.conn, 'radio_seed_offer')
        native = upgraded.conn.execute('SELECT * FROM radio_seed_output').fetchone()
        for field in ('menu_id', 'comparison_run_id', 'run_ref', 'run_resolution', 'content_digest', 'byte_size'):
            assert prepared[field] == native[field]
        for table, staged, fields in (
            ('radio_seed_subjects', preparation.SUBJECTS, 'position,video_id,entry_id,resolution_reason'),
            ('radio_seed_policies', preparation.POLICIES, 'raw_version,policy_version,resolution_reason'),
        ):
            assert list(map(tuple, upgraded.conn.execute(f'SELECT {fields} FROM {table} ORDER BY 1'))) == list(map(tuple,
                store.conn.execute(f'SELECT {fields} FROM {staged} ORDER BY 1')))
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_thousand_tracks_commit_bounded_members_and_keep_last_complete_output(installed):
    store, _ = installed
    sources, _ = seed(store)
    job = preparation.JOBS['radio-seed']
    job.install(store.conn)
    finish(job, store.conn)
    previous = head(store.conn, 'radio_seed_offer')
    value = {'menu_id': 'replacement-menu', 'tracks': [{'video_id': str(number % 9)} for number in range(1000)]}
    store.settings.set_setting('radio_seed_offer', json.dumps(value))
    for batch in range(8):
        before = store.conn.execute(f'SELECT count(*) FROM {preparation.SUBJECTS}').fetchone()[0]
        progress = job.step(store.conn, batch_size=128)
        after = store.conn.execute(f'SELECT count(*) FROM {preparation.SUBJECTS}').fetchone()[0]
        assert after - before == min(128, 1000 - batch * 128)
        if batch < 7:
            assert head(store.conn, 'radio_seed_offer')['preparation_revision'] == previous['preparation_revision']
    assert progress.status == 'prepared'
    current = head(store.conn, 'radio_seed_offer')
    assert json.loads(preparation.read_original(current)) == value
    assert preparation.read_original(previous) == sources['radio_seed_offer']
    assert [row[0] for row in store.conn.execute(f'SELECT video_id FROM {preparation.SUBJECTS} WHERE preparation_revision=? ORDER BY position',
                                               (current['preparation_revision'],))] == [str(number % 9) for number in range(1000)]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_reference_arrivals_and_retirement_reconcile_without_creating_or_blocking_identity(installed):
    store, _ = installed
    sources, run = seed(store)
    job = preparation.JOBS['radio-seed']
    job.install(store.conn)
    finish(job, store.conn)
    previous = head(store.conn, 'radio_seed_offer')
    entry = store.tracks.upsert_track('missing-video', 'Arrived', 'Artist', None, 100)
    store.rec_ledger.record_serving_policy('missing-policy', {'algorithm': 'arrived'}, 2)
    with store.conn:
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=?', (run,))
    assert job.progress(store.conn).prepared_revision is None
    finish(job, store.conn)
    current = head(store.conn, 'radio_seed_offer')
    assert (current['comparison_run_id'], current['run_ref'], current['run_resolution']) == (run, None, 'missing_run')
    assert store.conn.execute(f'SELECT entry_id FROM {preparation.SUBJECTS} WHERE preparation_revision=? AND position=1',
                              (current['preparation_revision'],)).fetchone()[0] == entry
    assert not store.conn.execute(f'SELECT 1 FROM {preparation.POLICIES} WHERE preparation_revision=? AND resolution_reason IS NOT NULL',
                                 (current['preparation_revision'],)).fetchone()
    assert preparation.read_original(previous) == sources['radio_seed_offer']
    assert not store.conn.execute('SELECT 1 FROM rec_model_selection_runs').fetchone()
    # Preparation must not add source FKs. The catalog's own evidence owners
    # retain this entry; staging must not impose an additional retirement gate.
    for table in (preparation.tables('radio_seed_offer')[0], preparation.SUBJECTS, preparation.POLICIES):
        assert all(row[2].startswith('prepared_') for row in store.conn.execute(f'PRAGMA foreign_key_list({table})'))
    with store.conn:
        store.conn.execute("DELETE FROM recommendation_policies WHERE version='missing-policy'")
    finish(job, store.conn)
    current = head(store.conn, 'radio_seed_offer')
    assert store.conn.execute(f'SELECT resolution_reason FROM {preparation.POLICIES} WHERE preparation_revision=? AND raw_version=\'missing-policy\'',
                              (current['preparation_revision'],)).fetchone()[0] == 'missing_policy'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_source_write_during_file_publication_rejects_stale_plan_and_retains_renamed_original(installed, monkeypatch):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['radio-seed']
    job.install(store.conn)
    real_publish = files.publish
    changed = False
    def write_during_file(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            store.settings.set_setting('radio_seed_offer', '{"menu_id":"new-source","tracks":[]}')
        return real_publish(root, encoded)
    monkeypatch.setattr(files, 'publish', write_during_file)
    assert job.step(store.conn).input_revision == 1
    assert head(store.conn, 'radio_seed_offer') is None
    finish(job, store.conn)
    retained = head(store.conn, 'radio_seed_offer')
    assert retained['menu_id'] == 'new-source'
    with store.conn:
        store.conn.execute("UPDATE settings SET key='retained-radio-original' WHERE key='radio_seed_offer'")
    finish(job, store.conn)
    assert head(store.conn, 'radio_seed_offer') is None
    assert json.loads(preparation.read_original(retained))['menu_id'] == 'new-source'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interruption_file_failure_independent_regular_ddl_and_reopened_retry(installed, preparation_ddl_connection, monkeypatch):
    store, _ = installed
    seed(store)
    radio, report = preparation.JOBS.values()
    for job in (radio, report):
        job.install(store.conn)
    checkpoint = radio.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled saved reference interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(radio, copy=interrupt).step(store.conn, batch_size=1)
    assert radio.progress(store.conn) == checkpoint
    real_publish = files.publish
    def disk_failure(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled saved file failure')
    monkeypatch.setattr(files, 'publish', disk_failure)
    with pytest.raises(OSError, match='file failure'):
        radio.step(store.conn)
    failed = radio.progress(store.conn)
    assert failed.status == 'failed'
    monkeypatch.setattr(files, 'publish', real_publish)
    assert finish(report, store.conn).status == 'prepared'
    def install(conn, context):
        conn.execute('CREATE TABLE independent_saved_output_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='independent_saved_output_ddl'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1,
        'independent_saved_output_ddl', install, validate)))
    regular.migrate(preparation_ddl_connection)
    assert store.conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
    assert radio.progress(store.conn) == failed
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            radio.step(reopened.conn)
        radio.step(reopened.conn, batch_size=1, retry=True)
        assert finish(radio, reopened.conn).status == 'prepared'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw,observed', [('historical non-JSON report', None), ('{"ran_at":true}', None),
    ('{"ran_at":1e999}', None), ('null', None), ('{"winner":{}}', None), ('{"ran_at":0}', 0)])
def test_historical_report_preserves_unreadable_bytes_without_inventing_observation_time(installed, raw, observed):
    store, _ = installed
    store.settings.set_setting('rec_autotune_result', raw)
    job = preparation.JOBS['autotune-report']
    job.install(store.conn)
    finish(job, store.conn)
    current = head(store.conn, 'rec_autotune_result')
    assert preparation.read_original(current) == raw
    assert current['observed_at'] == observed
    Path(current['filename']).write_text('controlled damaged file')
    with pytest.raises(ValueError, match='digest'):
        preparation.read_original(current)
    assert store.settings.get_setting('rec_autotune_result') == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw,kind', [(None, 'null'), ('', 'blank')])
def test_null_blank_missing_and_deletion_remain_distinct(installed, raw, kind):
    store, _ = installed
    for name, job in preparation.JOBS.items():
        source = preparation.SOURCES[name]
        job.install(store.conn)
        finish(job, store.conn)
        assert head(store.conn, source) is None
        # Current public writers normalize None to blank. A genuine retained
        # SQL NULL from the installed format still needs its own exact kind.
        with store.conn:
            store.conn.execute('INSERT INTO settings VALUES (?,?)', (source, raw))
        finish(job, store.conn)
        retained = head(store.conn, source)
        assert retained['source_kind'] == kind
        assert preparation.read_original(retained) == raw
        store.settings.delete_setting(source)
        finish(job, store.conn)
        assert head(store.conn, source) is None
        assert preparation.read_original(retained) == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw', ['null', '{"tracks":[{}]}', '{"tracks":{}}', '{"menu_id":7}',
    '{"comparison":{"run_id":true}}', '{"comparison":{"run_id":9223372036854775808}}',
    '{"tracks":[{"video_id":"v","pool_policy":false}]}', '{"tracks":' + json.dumps([{'video_id': 'v'}] * 1001) + '}',
    ' ' * (outputs.MAX_RAW_BYTES + 1)])
def test_invalid_radio_fails_durably_without_changing_source_or_other_job(installed, preparation_ddl_connection, raw):
    store, _ = installed
    conn = preparation_ddl_connection
    store.settings.set_setting('radio_seed_offer', raw)
    radio, report = preparation.JOBS.values()
    for job in (radio, report):
        job.install(conn)
    with pytest.raises((ValueError, MigrationError)):
        finish(radio, conn)
    assert radio.progress(conn).status == 'failed'
    assert store.settings.get_setting('radio_seed_offer') == raw
    assert head(conn, 'radio_seed_offer') is None
    assert finish(report, conn).status == 'prepared'
    regular.migrate(conn)
