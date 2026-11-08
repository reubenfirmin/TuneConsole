"""Model trials retain decisions and ordered baselines without blocking serving."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.core.test_predecessor_writes import installed
from tests.independent_schema_support import independent_regular_ddl
from yt_playlist.core import model_comparison_state as state
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import model_trials as preparation
from yt_playlist.core.store import Store


def seed(store):
    run = store.model_selection.start_selection(1, {'kind': 'product', 'previous': {'method': 'svd', 'dim': 2}})
    values = {'model_live_comparison': {'phase': 'observing', 'attempt': 2, 'seed': '0007',
        'policy': {'version': 'original', 'horizon_days': 7, 'family_alpha': .05},
        'previous': {'method': 'svd', 'dim': 2}, 'challenger': None, 'alpha': .025, 'run_id': run,
        'started_at': 10, 'exposure_until': 100, 'generation': 'captured-generation', 'vectors': 'captured-artifact'},
        'model_listening_monitor': {'run_id': 999, 'generation': 'prior-generation', 'setup': {'method': 'svd'},
            'baseline': [.1, .5, 1], 'complete': False, 'next_at': 300},
        'model_album_assignment': {'epoch': '0002', 'comparison': {'run_id': 999, 'arm': 'challenger',
            'format': 'albums', 'probability': .5, 'window': '002', 'unit': '0', 'controlled': True}}}
    originals = {key: json.dumps(value, ensure_ascii=False, indent=2) for key, value in values.items()}
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)', [(key, raw, None) for key, raw in originals.items()])
    return values, originals


def head(conn, surface):
    generations, current, _originals = preparation.tables(surface)
    return conn.execute(f'SELECT g.* FROM {current} h JOIN {generations} g USING(source,preparation_revision)').fetchone()


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
def test_independent_controls_retain_exact_inputs_and_explicit_run_custody(installed):
    store, _ = installed
    values, originals = seed(store)
    runs = list(map(tuple, store.conn.execute('SELECT * FROM rec_model_selection_runs')))
    for name, job in preparation.JOBS.items():
        job.install(store.conn)
        finish(job, store.conn)
        surface = preparation.SURFACES[name]
        row = head(store.conn, surface)
        archive = preparation.tables(surface)[2]
        assert store.conn.execute(f'SELECT raw_value,built_at FROM {archive} WHERE source=? AND digest=?',
                                  (surface, row['source_digest'])).fetchone()[:] == (originals[surface], None)
        assert {field: row[field] for field in state.parse(surface, values[surface])} == state.parse(surface, values[surface])
        assert row['run_ref'] == (1 if name == 'live-trial' else None)
        assert row['run_resolution'] == (None if name == 'live-trial' else 'missing_run')
        assert store.rec_proposals.get_proposals(surface) == values[surface]
        assert not store.conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (state.OWNERS[surface][0],)).fetchone()
    assert [row[0] for row in store.conn.execute(f'SELECT value FROM {preparation.BASELINE} ORDER BY position')] == [.1, .5, 1]
    assert list(map(tuple, store.conn.execute('SELECT * FROM rec_model_selection_runs'))) == runs
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [79, 94], indirect=True)
def test_destination_fields_match_frozen_v95_without_changing_the_upgrade_route(installed, tmp_path):
    store, _ = installed
    values, _ = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'trial-frozen-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:95])
        for surface, value in values.items():
            prepared = head(store.conn, surface)
            native = dict(upgraded.conn.execute('SELECT * FROM ' + state.OWNERS[surface][0]).fetchone())
            assert {key: prepared[key] for key in native if key != 'id'} == {key: val for key, val in native.items() if key != 'id'}
            assert state.read(upgraded.conn, surface) == value
        assert list(map(tuple, upgraded.conn.execute('SELECT position,value FROM model_monitor_baseline ORDER BY position'))) == [
            (row[0], row[1]) for row in store.conn.execute(f'SELECT position,value FROM {preparation.BASELINE} ORDER BY position')]
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_long_baseline_commits_bounded_members_and_retains_last_good_generation(installed):
    store, _ = installed
    values, _ = seed(store)
    job = preparation.JOBS['listening-monitor']
    surface = preparation.SURFACES['listening-monitor']
    job.install(store.conn)
    finish(job, store.conn)
    previous = head(store.conn, surface)
    baseline = [number / 4096 for number in range(4096)]
    store.rec_proposals.put_proposals(surface, {**values[surface], 'baseline': baseline}, 40)
    for _ in range(32):
        before = store.conn.execute(f'SELECT count(*) FROM {preparation.BASELINE}').fetchone()[0]
        progress = job.step(store.conn, batch_size=128)
        after = store.conn.execute(f'SELECT count(*) FROM {preparation.BASELINE}').fetchone()[0]
        assert after - before == 128
        if progress.status == 'prepared':
            break
        assert head(store.conn, surface)['preparation_revision'] == previous['preparation_revision']
    assert progress.status == 'prepared'
    current = head(store.conn, surface)
    assert current['preparation_revision'] != previous['preparation_revision']
    assert [row[0] for row in store.conn.execute(f'SELECT value FROM {preparation.BASELINE} WHERE preparation_revision=? ORDER BY position',
                                               (current['preparation_revision'],))] == baseline
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.BASELINE} WHERE preparation_revision=?',
                              (previous['preparation_revision'],)).fetchone()[0] == 3


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_source_and_run_changes_reconcile_without_admitting_identity(installed):
    store, _ = installed
    values, _ = seed(store)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    # A real source run becomes available later, without this preparation job
    # inventing one. The older unresolved generation remains retained.
    assignment = preparation.JOBS['album-assignment']
    surface = preparation.SURFACES['album-assignment']
    old = head(store.conn, surface)
    run = store.model_selection.start_selection(2, {'kind': 'product', 'previous': {'method': 'svd', 'dim': 3}})
    values[surface]['comparison']['run_id'] = run
    store.rec_proposals.put_proposals(surface, values[surface], 50)
    finish(assignment, store.conn)
    assert head(store.conn, surface)['run_ref'] == run
    assert old['run_ref'] is None and old['comparison_run_id'] == 999
    # Native source owners can still retire a referenced run during staging.
    with store.conn:
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=?', (run,))
    finish(assignment, store.conn)
    assert head(store.conn, surface)['run_ref'] is None
    assert head(store.conn, surface)['run_resolution'] == 'missing_run'
    with store.conn:
        store.conn.execute('UPDATE rec_proposals SET surface=? WHERE surface=?', ('retained-trial-source', surface))
    finish(assignment, store.conn)
    assert head(store.conn, surface) is None
    assert store.conn.execute('SELECT count(*) FROM ' + preparation.tables(surface)[2]).fetchone()[0] == 2


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_concurrent_edit_between_planning_and_checkpoint_rejects_stale_source(installed):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['live-trial']
    job.install(store.conn)
    def update_source(conn, row):
        assert not conn.in_transaction
        plan = preparation.prepare(conn, row)
        store.rec_proposals.put_proposals('model_live_comparison', {'phase': 'retained', 'reason': 'user decision'}, 55)
        return plan
    assert replace(job, prepare=update_source).step(store.conn).input_revision == 1
    assert head(store.conn, 'model_live_comparison') is None
    finish(job, store.conn)
    assert head(store.conn, 'model_live_comparison')['phase'] == 'retained'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interruption_durable_failure_independent_new_ddl_and_reopened_retry(installed, monkeypatch, independent_regular_ddl):
    store, _ = installed
    values, _ = seed(store)
    job = preparation.JOBS['listening-monitor']
    for selected in preparation.JOBS.values():
        selected.install(store.conn)
    checkpoint = job.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled baseline interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupt).step(store.conn, batch_size=1)
    assert job.progress(store.conn) == checkpoint
    store.rec_proposals.put_proposals('model_listening_monitor', {'baseline': [2]}, 70)
    with pytest.raises(ValueError, match='baseline'):
        job.step(store.conn)
    failed = job.progress(store.conn)
    assert failed.status == 'failed'
    assert finish(preparation.JOBS['live-trial'], store.conn).status == 'prepared'
    assert finish(preparation.JOBS['album-assignment'], store.conn).status == 'prepared'
    regular.migrate(store.conn)
    assert store.conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
    assert job.progress(store.conn) == failed
    store.rec_proposals.put_proposals('model_listening_monitor', values['model_listening_monitor'], 80)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, batch_size=1, retry=True)
        assert finish(job, reopened.conn).status == 'prepared'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('value', [{}, {'baseline': None}, {'baseline': []}])
def test_absent_null_and_empty_baselines_preserve_masks_and_originals(installed, value):
    store, _ = installed
    store.rec_proposals.put_proposals('model_listening_monitor', value, None)
    job = preparation.JOBS['listening-monitor']
    job.install(store.conn)
    finish(job, store.conn)
    row = head(store.conn, 'model_listening_monitor')
    assert row['field_mask'] == state.parse('model_listening_monitor', value)['field_mask']
    assert row['baseline_present'] == ('baseline' in value and value['baseline'] is not None)
    assert not store.conn.execute(f'SELECT 1 FROM {preparation.BASELINE}').fetchone()
    assert store.rec_proposals.get_proposals('model_listening_monitor') == value


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name,raw', [
    ('live-trial', '{"phase":"unknown"}'), ('live-trial', '{"attempt":true}'),
    ('live-trial', '{"unclassified":1}'), ('live-trial', '{"alpha":NaN}'),
    ('listening-monitor', '{"baseline":[2]}'), ('listening-monitor', '{"baseline":' + json.dumps([0] * 4097) + '}'),
    ('album-assignment', '{"comparison":{"probability":2}}'), ('album-assignment', 'null'),
    ('live-trial', ' ' * (preparation.MAX_BYTES + 1)),
])
def test_invalid_or_oversized_documents_fail_durably_and_leave_ordinary_source_usable(installed, name, raw):
    store, _ = installed
    surface = preparation.SURFACES[name]
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,NULL)', (surface, raw))
    job = preparation.JOBS[name]
    job.install(store.conn)
    with pytest.raises((ValueError, MigrationError, sqlite3.IntegrityError)):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == raw
    assert head(store.conn, surface) is None
