"""Retained taste evidence is prepared independently of serving and numbered DDL."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.prepared_fixture_support import assert_upgrade_phase, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import publication_consumer_reads
from tests.core.test_taste_controls_migration import decision
from yt_playlist.core import collection_recovery, policy_documents, recommendation_outputs as files, taste_control_state
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import taste_controls as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Taste preparation checks do not construct an HTTP application."""


@pytest.mark.parametrize('version', [79, 93, 95])
def test_taste_input_keeps_each_installed_contract_and_snapshot_guards(_bounded_installed_schema, version):
    from contextlib import closing

    template = _bounded_installed_schema(version)
    source = template.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, version)
        regular.validate(reader)
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert not source.conn.execute('SELECT 1 FROM main.recordings').fetchone()
    assert template.revision_guards


def finish(job, conn, size=128):
    for _ in range(100):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Taste preparation did not finish bounded fixture')


def seed(store, count=3):
    store.tracks.upsert_track('known-video', 'Known', 'Artist', None, 100)
    checkpoint = {'saved_at': 5, 'params': {'rec_param:breadth_bias': '.3', 'historical': '東京'},
                  'weights': [{'axis': 'genre:An unknown original', 'weight': .4, 'updated_at': None},
                              {'axis': 'lane:wheelhouse', 'weight': -.2, 'updated_at': 0}]}
    baseline = [{'key': 'known|artist' if index == 0 else f'unknown:{index}', 'rank': None if index == 0 else index + 1,
                 'score': .2 + index, 'lane': '', 'lane_weight': None, 'trace': {'term': index, 'label': '東京'}}
                for index in range(count)]
    values = {preparation.CHECKPOINT: checkpoint, preparation.BASELINE: baseline,
              preparation.DECISIONS: [decision(6), {**decision(7), 'decision': 'promote', 'powered': True,
                  'live_evidence': {'ppr': {'result': [None, 1, '東京']}, 'cosine': None}}]}
    originals = {source: json.dumps(value, ensure_ascii=False, indent=2) for source, value in values.items()}
    with store.conn:
        store.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', originals.items())
    return values, originals


def members(conn, source, suffix, columns='*'):
    head = preparation.head(conn, source)
    return conn.execute(f'SELECT {columns} FROM {preparation.table(source,suffix)} '
        'WHERE source=? AND preparation_revision=? ORDER BY position', (source, head['preparation_revision'])).fetchall()


@pytest.mark.parametrize('installed', [79, 93, 95], indirect=True)
def test_three_jobs_keep_exact_settings_ordered_evidence_and_unadmitted_references(installed):
    store, version = installed
    values, originals = seed(store)
    axes = list(map(tuple, store.conn.execute('SELECT * FROM taste_axes'))) if version >= 88 else None
    recordings = list(map(tuple, store.conn.execute('SELECT * FROM recordings')))
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn, size=2)
    for source, raw in originals.items():
        head = preparation.head(store.conn, source)
        assert preparation.read_original(store.conn, head) == raw
        assert store.settings.get_setting(source) == raw
        assert head['member_count'] == (2 if source != preparation.BASELINE else 3)
        assert all(row[2].startswith('prepared_') for row in store.conn.execute(
            f'PRAGMA foreign_key_list({preparation.table(source,"generations")})'))
    checkpoint = preparation.head(store.conn, preparation.CHECKPOINT)
    assert checkpoint['saved_at'] == 5
    assert collection_recovery.read(checkpoint['recovery_filename'], checkpoint['recovery_digest'], checkpoint['recovery_bytes'],
                                    kind='taste-checkpoint', owner=1) == values[preparation.CHECKPOINT]['params']
    assert tuple(members(store.conn, preparation.CHECKPOINT, 'weights', 'kind,value,resolution_reason,axis_ref')[0]) == (
        'unresolved_genre', 'An unknown original', 'unknown_genre', None)
    baseline = preparation.head(store.conn, preparation.BASELINE)
    assert collection_recovery.read(baseline['recovery_filename'], baseline['recovery_digest'], baseline['recovery_bytes'],
        kind='taste-comparison', owner=1) == [item['trace'] for item in values[preparation.BASELINE]]
    entries = members(store.conn, preparation.BASELINE, 'entries', 'captured_key,recording_id,unresolved_key,rank,lane,lane_weight')
    assert entries[0]['recording_id'] is not None and tuple(entries[0])[2:] == (None, None, '', None)
    assert tuple(entries[1])[1:3] == (None, 'unknown:1')
    for row, expected in zip(members(store.conn, preparation.DECISIONS, 'decisions'), values[preparation.DECISIONS]):
        assert collection_recovery.read(row['recovery_filename'], row['recovery_digest'], row['recovery_bytes'],
            kind='experiment-decision', owner=row['position'] + 1) == expected['live_evidence']
        assert row['created_at'] == expected['at'] and row['powered'] == int(expected['powered'])
    assert recordings == list(map(tuple, store.conn.execute('SELECT * FROM recordings')))
    if axes is not None:
        assert axes == list(map(tuple, store.conn.execute('SELECT * FROM taste_axes')))
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare_native_comparison(store):
    values, originals = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    return values, originals


@pytest.fixture(scope='module')
def native_inputs(upgraded_custody_inputs):
    def get(version, target, **options):
        return upgraded_custody_inputs(version, target, 'taste-native-agreement',
            prepare_native_comparison, **options)

    get.release = lambda version: upgraded_custody_inputs.release(version, 'taste-native-agreement')
    return get


def assert_native_originals(template):
    for source, raw in template.result[1].items():
        assert preparation.read_original(template.source.conn, preparation.head(template.source.conn, source)) == raw


@pytest.mark.parametrize('version', [79, 95])
def test_native_comparison_input_keeps_exact_preparation(native_inputs, version):
    template = native_inputs(version, version)
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert_native_originals(template)


@pytest.mark.parametrize('version,target,phase', sorted(
    custody_upgrade_cases((79, 95)) + [(version, 96, phase) for version in (79, 95) for phase in ('input', 'converted')],
    key=lambda case: (case[0], case[1], case[2] == 'converted')))
def test_native_upgrade_phase_keeps_exact_preparation(native_inputs, version, target, phase):
    template = native_inputs(version, target, phase=phase)
    assert_upgrade_phase(template, target if phase == 'converted' else custody_predecessor(version, target))
    assert_native_originals(template)


@pytest.mark.parametrize('version', [79, 95])
def test_native_serving_input_keeps_private_read_guards(native_inputs, version):
    template = native_inputs(version, 107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn


@pytest.mark.parametrize('version', [79, 95])
def test_frozen_v96_and_full_upgrade_match_prepared_fields_and_keep_archives(native_inputs, version):
    prepared = native_inputs(version, version)
    store = prepared.source
    values, _ = prepared.result
    upgraded = native_inputs(version, 96).source
    for source in (preparation.CHECKPOINT, preparation.BASELINE):
        assert json.loads(taste_control_state.get(upgraded.conn, source)) == values[source]
    native = list(map(tuple, upgraded.conn.execute('SELECT position,recording_id,unresolved_key,captured_key,rank,score,lane,lane_weight '
                                                   'FROM taste_comparison_entries ORDER BY position')))
    assert native == list(map(tuple, members(store.conn, preparation.BASELINE, 'entries',
        'position,recording_id,unresolved_key,captured_key,rank,score,lane,lane_weight')))
    from yt_playlist.core import experiment_decision_state
    assert experiment_decision_state.records(upgraded.conn) == values[preparation.DECISIONS]
    assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', [79, 95])
def test_native_upgrade_keeps_exact_preparation_after_file_collection(native_inputs, version, tmp_path):
    _, originals = native_inputs(version, version).result
    with native_inputs(version, 107).open(tmp_path / 'taste-upgrade.db') as upgraded:
        assert upgraded.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
        upgraded.conn.execute('PRAGMA wal_autocheckpoint=0')
        collection_recovery.collect_orphans(upgraded.conn)
        for source, raw in originals.items():
            assert preparation.read_original(upgraded.conn, preparation.head(upgraded.conn, source)) == raw
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_baseline_resumes_without_source_payload_reads_and_retains_last_complete_generation(installed, monkeypatch):
    store, _ = installed
    _, originals = seed(store, count=1040)
    job = preparation.JOBS['baseline']
    job.install(store.conn)
    first = job.step(store.conn, batch_size=128)
    assert first.processed == 128 and preparation.head(store.conn, preparation.BASELINE) is None
    def authorizer(action, owner, column, *unused):
        if action == sqlite3.SQLITE_READ and owner == 'settings' and column == 'value':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(authorizer)
    try:
        while isinstance(json.loads(job.progress(store.conn).cursor), dict):
            job.step(store.conn)
    finally:
        store.conn.set_authorizer(None)
    finish(job, store.conn)
    old = preparation.head(store.conn, preparation.BASELINE)
    assert len(members(store.conn, preparation.BASELINE, 'entries')) == 1040
    with store.conn:
        store.conn.execute('UPDATE settings SET value=? WHERE key=?',
            (json.dumps([{'key': 'replacement', 'rank': 1, 'score': 3, 'lane': '', 'lane_weight': None, 'trace': None}]), preparation.BASELINE))
    job.step(store.conn, batch_size=1)
    assert preparation.head(store.conn, preparation.BASELINE)['preparation_revision'] == old['preparation_revision']
    finish(job, store.conn)
    assert preparation.read_original(store.conn, old) == originals[preparation.BASELINE]
    assert members(store.conn, preparation.BASELINE, 'entries', 'captured_key')[0][0] == 'replacement'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_source_and_catalog_writes_during_file_work_reject_stale_plan(installed, monkeypatch):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['baseline']
    job.install(store.conn)
    publish, changed = files.publish, False
    def race(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            with store.conn:
                store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('unknown:1')")
        return publish(root, encoded)
    monkeypatch.setattr(files, 'publish', race)
    assert job.step(store.conn).processed == 0
    assert preparation.head(store.conn, preparation.BASELINE) is None
    finish(job, store.conn)
    old = preparation.head(store.conn, preparation.BASELINE)
    assert members(store.conn, preparation.BASELINE, 'entries', 'recording_id')[1][0] is not None
    with store.conn:
        store.conn.execute('DELETE FROM settings WHERE key=?', (preparation.BASELINE,))
    finish(job, store.conn)
    assert preparation.head(store.conn, preparation.BASELINE) is None
    assert preparation.read_original(store.conn, old)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_failure_interruption_explicit_retry_and_independent_regular_ddl(installed, monkeypatch):
    store, _ = installed
    seed(store)
    job, independent = preparation.JOBS['baseline'], preparation.JOBS['decisions']
    for target in (job, independent):
        target.install(store.conn)
    checkpoint = job.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled taste interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupt).step(store.conn, batch_size=1)
    assert job.progress(store.conn) == checkpoint
    row = store.conn.execute('SELECT plan_filename FROM prepared_taste_comparison_generations').fetchone()
    Path(row[0]).unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn)
    failed = job.progress(store.conn)
    assert failed.status == 'failed'
    def install(conn, context):
        conn.execute('CREATE TABLE independent_taste_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_taste_ddl'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (regular.Change(1, 'independent_taste_ddl', install, validate),))
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    finish(independent, store.conn)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_recovery_disk_failure_does_not_change_settings_or_other_jobs(installed, monkeypatch):
    store, _ = installed
    values, originals = seed(store)
    checkpoint, baseline = preparation.JOBS['checkpoint'], preparation.JOBS['baseline']
    for job in (checkpoint, baseline):
        job.install(store.conn)
    settings = dict(store.conn.execute('SELECT key,CAST(value AS BLOB) FROM settings'))
    publish = policy_documents._publish_encoded
    failures = []
    def failed(root, encoded, digest, *, age=None):
        assert not store.conn.in_transaction
        failures.append((root, digest, age))
        raise OSError('controlled taste disk failure')
    monkeypatch.setattr(policy_documents, '_publish_encoded', failed)
    with pytest.raises(OSError, match='controlled taste disk failure'):
        checkpoint.step(store.conn)
    assert len(failures) == 1
    failed_progress = checkpoint.progress(store.conn)
    assert failed_progress.status == 'failed'
    assert preparation.head(store.conn, preparation.CHECKPOINT) is None
    assert dict(store.conn.execute('SELECT key,CAST(value AS BLOB) FROM settings')) == settings
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    finish(baseline, store.conn)
    assert checkpoint.progress(store.conn) == failed_progress
    with pytest.raises(MigrationError, match='retry explicitly'):
        checkpoint.step(store.conn)
    checkpoint.step(store.conn, retry=True)
    finish(checkpoint, store.conn)
    for job, source, kind, expected in (
            (baseline, preparation.BASELINE, 'taste-comparison',
             [item['trace'] for item in values[preparation.BASELINE]]),
            (checkpoint, preparation.CHECKPOINT, 'taste-checkpoint',
             values[preparation.CHECKPOINT]['params'])):
        assert job.progress(store.conn).status == 'prepared'
        head = preparation.head(store.conn, source)
        assert preparation.read_original(store.conn, head) == originals[source]
        assert collection_recovery.read(head['recovery_filename'], head['recovery_digest'], head['recovery_bytes'],
                                        kind=kind, owner=1) == expected
    assert len(members(store.conn, preparation.BASELINE, 'entries')) == 3
    assert len(members(store.conn, preparation.CHECKPOINT, 'weights')) == 2
    assert dict(store.conn.execute('SELECT key,CAST(value AS BLOB) FROM settings')) == settings
    assert dict(store.conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?)', tuple(originals))) == originals
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_absent_null_blank_and_empty_states_remain_distinct(installed):
    store, _ = installed
    for name, source in preparation.SOURCES.items():
        job = preparation.JOBS[name]
        job.install(store.conn)
        finish(job, store.conn)
        assert preparation.head(store.conn, source) is None
        for raw in (None, '', '[]' if name != 'checkpoint' else 'null'):
            with store.conn:
                store.conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (source, raw))
            finish(job, store.conn)
            head = preparation.head(store.conn, source)
            assert preparation.read_original(store.conn, head) == raw
            assert head['source_kind'] == ('null' if raw is None else 'blank' if raw == '' else 'recorded')
            assert head['member_count'] == 0
            assert head['state_present'] == int(name == 'decisions' or raw == '[]')


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_invalid_and_conflicting_controls_fail_without_selecting_a_new_generation(installed):
    store, _ = installed
    values, originals = seed(store)
    for name, source in preparation.SOURCES.items():
        job = preparation.JOBS[name]
        job.install(store.conn)
        finish(job, store.conn)
        old = preparation.head(store.conn, source)
        invalid = ({**values[source], 'weights': values[source]['weights'] * 2} if name == 'checkpoint'
                   else values[source] * (101 if name == 'decisions' else 2))
        for raw in (json.dumps(invalid), '{broken'):
            with store.conn:
                store.conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, source))
            with pytest.raises(ValueError):
                job.step(store.conn, retry=True)
            assert job.progress(store.conn).status == 'failed'
            assert preparation.head(store.conn, source)['preparation_revision'] == old['preparation_revision']
            assert preparation.read_original(store.conn, old) == originals[source]
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (originals[source], source))
        job.step(store.conn, retry=True)
        finish(job, store.conn)


@pytest.mark.parametrize('version', [79, 95])
def test_native_sources_finish_with_committed_read_only_inputs(native_inputs, version):
    for source in native_inputs.release(version):
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')


@pytest.mark.parametrize('installed', [85], indirect=True)
def test_upgrade_adds_optional_reference_capture_before_reusing_proof(installed, tmp_path):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['checkpoint']
    job.install(store.conn)
    finish(job, store.conn)
    upgraded = Store(tmp_path / 'taste-optional-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:88])
        before = job.progress(upgraded.conn)
        with pytest.raises(MigrationError, match='Incompatible preparation object'):
            job.step(upgraded.conn)
        assert job.progress(upgraded.conn) == before
        current = job.install(upgraded.conn)
        assert current.prepared_revision is None and current.input_revision == before.input_revision + 1
        finish(job, upgraded.conn)
        from yt_playlist.core import taste_axes
        with upgraded.conn:
            axis = taste_axes.identity(upgraded.conn, 'genre:An unknown original', create=True)
        assert job.progress(upgraded.conn).prepared_revision is None
        finish(job, upgraded.conn)
        assert members(upgraded.conn, preparation.CHECKPOINT, 'weights', 'axis_ref')[0][0] == axis
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_alias_edits_capture_new_interpretation_without_rewriting_earlier_weights(installed):
    store, _ = installed
    _, originals = seed(store)
    job = preparation.JOBS['checkpoint']
    job.install(store.conn)
    finish(job, store.conn)
    old = preparation.head(store.conn, preparation.CHECKPOINT)
    with store.conn:
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('explicit-style','Explicit Style','custom')")
        store.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES "
                           "('an unknown original','An unknown original','explicit-style','custom')")
    assert job.progress(store.conn).prepared_revision is None
    finish(job, store.conn)
    assert tuple(members(store.conn, preparation.CHECKPOINT, 'weights', 'kind,genre_key,value,resolution_reason,axis_ref')[0]) == (
        'genre', 'explicit-style', None, None, None)
    previous = store.conn.execute('SELECT kind,value FROM prepared_taste_checkpoint_weights '
        'WHERE preparation_revision=? ORDER BY position', (old['preparation_revision'],)).fetchone()
    assert tuple(previous) == ('unresolved_genre', 'An unknown original')
    assert preparation.read_original(store.conn, old) == originals[preparation.CHECKPOINT]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_invalid_clocks_ranks_and_evidence_fail_with_original_sources_retained(installed):
    store, _ = installed
    values, originals = seed(store)
    bad = {
        'checkpoint': [{**values[preparation.CHECKPOINT], 'saved_at': None},
                       {**values[preparation.CHECKPOINT], 'saved_at': float('inf')},
                       {**values[preparation.CHECKPOINT], 'params': {'x': 1}}],
        'baseline': [[{**values[preparation.BASELINE][0], 'rank': True}],
                     [{**values[preparation.BASELINE][0], 'score': '2'}],
                     [{**values[preparation.BASELINE][0], 'trace': float('nan')}]],
        'decisions': [[{**decision(1), 'powered': 1}], [{**decision(1), 'live_evidence': {'ppr': None}}],
                      [{**decision(None)}]],
    }
    for name, invalid in bad.items():
        source, job = preparation.SOURCES[name], preparation.JOBS[name]
        job.install(store.conn)
        for value in invalid:
            raw = json.dumps(value)
            with store.conn:
                store.conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, source))
            with pytest.raises(ValueError):
                job.step(store.conn, retry=True)
            assert preparation.head(store.conn, source) is None
            assert store.conn.execute('SELECT value FROM settings WHERE key=?', (source,)).fetchone()[0] == raw
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (originals[source], source))
        job.step(store.conn, retry=True)
        finish(job, store.conn)
