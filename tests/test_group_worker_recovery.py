"""Storage failures must leave the scheduled worker able to serve and retry."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.group_support import recordings
from tests.rec.test_home_prepared import one_menu_per_attempt
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, native_sql_calls,
)
from tests.test_group_preparation import seed
from tests.test_mode_refresh_worker import scheduled
from yt_playlist.core import schema_inspection
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.rec import (album_choices, album_notes, choice_evidence, group_preparation,
                             group_serving, home_cards, home_prepared, measurement, rec_worker)
from yt_playlist.rec.worker_queue import Job


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These explicit worker turns do not start the HTTP app's periodic sync."""


@pytest.fixture(scope='module')
def worker_schema():
    with bounded_consumer_schema(entries=(('house', 'House'),), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def store(worker_schema, tmp_path, monkeypatch):
    database = Store(tmp_path / 'recovery.db')
    try:
        database.conn.execute('PRAGMA synchronous=OFF')
        with schema_inspection.reuse_tables(database.conn):
            initialize_consumer(database, worker_schema, snapshot_guards=False)
            # Queue recovery owns persistence failures and real group/Home
            # publication. Snapshot conflict detection has separate owners.
            with mock_snapshot_coordinator(database, monkeypatch):
                yield database
    finally:
        database.conn.close()


def test_worker_recovery_schema_keeps_real_history_and_readonly_input(worker_schema):
    from yt_playlist.core.migrations import registry

    assert worker_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert worker_schema.execute('SELECT version, name FROM schema_migrations ORDER BY version').fetchall() == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS]
    assert worker_schema.execute('PRAGMA foreign_key_check').fetchall() == []
    assert worker_schema.execute('SELECT key, name FROM genre_catalog').fetchall() == [('house', 'House')]


def prepare_previous_groups(store):
    seed(store, recordings([('House', 3, .1)]))
    assert group_preparation.Preparation(store).step(1000)
    assert group_serving.prepare(store, 1000)
    previous = (store.taste_groups.head(members=True),
                store.rec_proposals.get_proposals(group_serving.SURFACE), group_preparation.status(store))
    seed(store, recordings([('House', 4, .1)]))
    return previous


@pytest.fixture
def previous_groups(store):
    return prepare_previous_groups(store)


@pytest.fixture
def completed_home_inputs(scheduled, monkeypatch):
    """Recovery owns real Home publication, independent of albums and diagnostics."""
    # The shared queue fixture stubs Home. This workflow owns its real publisher.
    monkeypatch.delattr(scheduled[0].tasks, 'home')
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args, **kwargs: True)
    monkeypatch.setattr(choice_evidence, 'prepare_context', lambda *args, **kwargs: True)
    monkeypatch.setattr(choice_evidence, 'refresh_report', lambda *args, **kwargs: {})
    monkeypatch.setattr(choice_evidence, 'model_context', lambda *args, **kwargs: {})
    monkeypatch.setattr(choice_evidence, 'freeze_context', lambda *args, **kwargs: {})
    monkeypatch.setattr(album_choices, 'prepare_cached', lambda *args, **kwargs: {'items': []})
    monkeypatch.setattr(album_notes, 'prepare', lambda *args, **kwargs: False)
    # These independent summaries do not participate in the failed group job.
    # Home still builds and publishes its actual mix payload on every turn.
    monkeypatch.setattr(home_prepared, 'prepare_discovery', lambda *args, **kwargs: None)
    monkeypatch.setattr(home_prepared, 'prepare_revisit', lambda *args, **kwargs: None)
    monkeypatch.setattr(home_cards.HomeCards, 'cards', lambda self, request: home_cards.CardsResult(
        [], [], None, None, [], [], request.epoch, None))


@pytest.mark.parametrize('failure', ['initial-progress-write', 'yield-report-write'])
def test_group_progress_storage_failure_retries_and_runs_real_home(
        scheduled, previous_groups, completed_home_inputs, one_menu_per_attempt, monkeypatch, caplog, failure):
    worker, targets = scheduled
    store = worker.ctx.store
    previous, preview, progress = previous_groups
    clock, failing, attempts, steps, homes = [100], [True], [], [], []
    monkeypatch.setattr(rec_worker, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    worker.ctx.now_fn = lambda: 1000 + clock[0]
    worker.ctx.client_provider = lambda: {}
    # Keep this recovery workflow local; TV and story providers have other checks.
    monkeypatch.setattr(worker, 'trigger_tv_refresh', lambda: False)
    monkeypatch.setattr(worker.tasks, 'stories', lambda: False)
    setting, step, home = store.settings.set_setting, worker.preparation.groups.step, worker.tasks.home

    def storage_full(key, value):
        if key == group_preparation.PROGRESS and failing[0]:
            phase = json.loads(value)['phase']
            attempts.append(phase)
            if phase != 'preparing':
                worker.trigger_home_refresh(force=True)
            error = sqlite3.OperationalError('database or disk is full')
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error
        return setting(key, value)

    def grouping(now):
        steps.append(now)
        if failure == 'yield-report-write' and failing[0]:
            raise WorkBudgetExceeded('controlled serving-work arrival')
        return step(now)

    def prepare_home():
        result = home()
        homes.append(store.rec_proposals.get_proposals(home_prepared.SURFACE))
        return result

    monkeypatch.setattr(store.settings, 'set_setting', storage_full)
    monkeypatch.setattr(worker.preparation.groups, 'step', grouping)
    monkeypatch.setattr(worker.tasks, 'home', prepare_home)
    state = worker.jobs.retry[Job.GROUPS]
    expected = ['preparing', 'failed'] if failure == 'initial-progress-write' else ['deferred']
    for count, delay in ((1, 60), (2, 120)):
        worker._schedule(full=False, groups=True)
        assert len(targets) == 1
        targets.pop()()
        assert not worker._running and not worker.work.active and not targets
        assert state.dirty and state.failures == count
        assert state.retry_at == clock[0] + delay
        assert attempts == expected * count
        assert len(homes) == count and homes[-1]['built_at'] == worker.ctx.now_fn()
        assert store.taste_groups.head(members=True) == previous
        assert store.rec_proposals.get_proposals(group_serving.SURFACE) == preview
        assert group_preparation.status(store) == progress
        # A forced early group turn does no preparation or repeated reporting.
        worker._schedule(full=False, groups=True)
        targets.pop()()
        assert len(steps) == count and attempts == expected * count
        assert state.dirty and not targets
        clock[0] = state.retry_at

    assert 'Could not persist adaptive grouping' in caplog.text
    assert any(isinstance(record.exc_info[1], sqlite3.OperationalError)
               for record in caplog.records if record.exc_info)
    failing[0] = False
    # Exercise the real poll for the group retry without requesting unrelated fits.
    for job in (Job.PREPARE, Job.FULL, Job.BUNDLES):
        worker.jobs.retry[job].retry_at = float('inf')
    worker._poll_refresh()
    worker.trigger_home_refresh(force=True)
    assert len(targets) == 1
    targets.pop()()
    assert len(steps) == 3 and len(homes) >= 3
    assert homes[-1]['built_at'] == worker.ctx.now_fn()
    assert group_preparation.status(store)['phase'] == 'ready'
    assert not state.dirty and state.failures == 0
    assert not worker._running and not worker.work.active and not targets
    current = store.taste_groups.head(members=True)
    assert current['groups'] and current['generation'] != previous['generation']
    assert sum(len(group['members']) for group in current['groups']) == 4
    assert store.taste_groups.definitions(previous['generation'], members=True) == previous['groups']
    assert group_serving.view(store)['generation'] == store.taste_groups.head()['generation']


@pytest.mark.parametrize('failure', ['preparation', 'failure-report', 'deferred-report'])
def test_group_progress_shutdown_cancels_queued_work(scheduled, monkeypatch, failure):
    worker, targets = scheduled
    calls = []

    def grouping(now):
        worker.trigger_home_refresh(force=True)
        worker.work.close_admission()
        if failure == 'preparation':
            raise WorkClosing('controlled shutdown')
        if failure == 'failure-report':
            raise sqlite3.OperationalError('controlled storage failure')
        raise WorkBudgetExceeded('controlled serving-work arrival')

    def report(*args, **kwargs):
        raise WorkClosing('controlled shutdown during progress persistence')

    monkeypatch.setattr(worker.preparation.groups, 'step', grouping)
    monkeypatch.setattr(group_preparation, 'report', report)
    monkeypatch.setattr(worker.tasks, 'home', lambda: calls.append('home'))
    worker._schedule(full=False, groups=True)
    targets.pop()()
    assert worker._stop.is_set() and not worker._running
    assert not worker.jobs and not worker.work.active and not calls
    assert not any(state.dirty for state in worker.jobs.retry.values())
    worker.trigger_home_refresh(force=True)
    assert not targets


@pytest.mark.parametrize('error', [sqlite3.OperationalError, SystemExit])
def test_unexpected_worker_exit_releases_ownership_and_retains_arrivals(scheduled, monkeypatch, caplog, error):
    worker, targets = scheduled
    calls = []
    original = worker._run_pending

    def fail_with_arrivals():
        worker.trigger_home_refresh(force=True)
        raise error('controlled unexpected worker failure')

    monkeypatch.setattr(worker, '_run_pending', fail_with_arrivals)
    worker._schedule(full=False, groups=True)
    if issubclass(error, Exception):
        targets.pop()()
        assert 'pending work retained' in caplog.text
    else:
        with pytest.raises(error, match='controlled unexpected worker failure'):
            targets.pop()()
    assert not worker._running and not worker.work.active
    assert worker.jobs.has(Job.HOME) and worker.jobs.has(Job.GROUPS)
    monkeypatch.setattr(worker, '_run_pending', original)
    monkeypatch.setattr(worker.tasks, 'home', lambda: calls.append('home') or False)
    monkeypatch.setattr(worker.tasks, 'stories', lambda: False)
    monkeypatch.setattr(worker, 'trigger_tv_refresh', lambda: False)
    monkeypatch.setattr(worker.preparation.groups, 'step', lambda now: calls.append('groups'))
    worker.trigger_home_refresh(force=True)
    assert len(targets) == 1
    targets.pop()()
    assert calls == ['home', 'groups']
    assert not worker._running and not worker.jobs and not worker.work.active
