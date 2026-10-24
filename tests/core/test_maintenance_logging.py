"""Routine maintenance stays quiet while progress, source guards and failures stay observable."""
import logging
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_diagnostics, work_diagnostics
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.rec import home_prepared, mode_surfaces
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.worker_tasks import ServingTasks


@pytest.mark.parametrize('publication_threshold', [0., float('inf')])
def test_publication_summaries_are_bounded_and_keep_phase_reason_and_progress_counts(
        store, caplog, monkeypatch, publication_threshold):
    monkeypatch.setattr(db_diagnostics, 'SLOW_LOCK_SECONDS', publication_threshold)
    clock = [0.0]
    store.maintenance_diagnostics = work_diagnostics.MaintenanceDiagnostics(clock=lambda: clock[0])
    store.settings.set_setting('input', 'initial')
    caplog.set_level(logging.DEBUG)
    for window in range(3):
        for attempt in range(60):
            clock[0] = window * 60 + attempt
            with work_diagnostics.work_span('rec: cluster surfaces and PPR', announce=True, level=logging.DEBUG):
                with store.read_snapshot() as snapshot:
                    snapshot.store.settings.get_setting('input')
                    if attempt % 2 == 0:
                        store.settings.set_setting('input', str(clock[0]))
                    published = snapshot.publish(lambda: store.rec_proposals.publish_prepared_home(
                        str(int(clock[0])), clock[0], surface='logging-probe'))
                    assert published == (attempt % 2 == 1)
            store.maintenance_diagnostics.record('deferred', phase='rec: Home collections',
                                                 reasons=('time_budget',))
        clock[0] = (window + 1) * 60
        store.maintenance_diagnostics.flush()
    summaries = [r for r in caplog.records if r.name == work_diagnostics.logger.name
                 and r.getMessage().startswith('Maintenance summary:')]
    assert len(summaries) == 3
    for index, record in enumerate(summaries):
        elapsed, published, deferred, failed, phases, reasons, waiting = record.args
        assert (elapsed, published, deferred, failed) == (60, 30, 90, 0)
        assert phases == {
            'rec: cluster surfaces and PPR': {'published': 30, 'deferred': 30},
            'rec: Home collections': {'deferred': 60},
        }
        assert reasons == {
            'rec: cluster surfaces and PPR': {'source:settings': 30},
            'rec: Home collections': {'time_budget': 60},
        }
        assert waiting == {'rec: Home collections': (index + 1) * 60}
    assert store.rec_proposals.get_proposals('logging-probe') == 179
    assert any('Maintenance publication deferred:' in r.message and r.levelno == logging.DEBUG
               for r in caplog.records)
    assert any('work start:' in r.message and r.levelno == logging.DEBUG for r in caplog.records)
    assert any(r.name == db_diagnostics.logger.name and r.levelno == logging.INFO
               and r.getMessage().startswith('Maintenance publication:')
               for r in caplog.records) == (publication_threshold == 0.)


def test_home_budget_is_counted_once_and_keeps_last_good_collections(store, monkeypatch, caplog):
    clock = [0.0]
    store.maintenance_diagnostics = work_diagnostics.MaintenanceDiagnostics(clock=lambda: clock[0])
    store.rec_proposals.put_proposals('home_prepared', {'last-good': True}, 0)

    def exhausted(*args):
        raise WorkBudgetExceeded('budget exhausted')

    monkeypatch.setattr(home_prepared, '_prepare', exhausted)
    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1000, client_provider=lambda: {},
        logger=logging.getLogger('test')), lambda name, target, *args, **kwargs: target(*args, **kwargs))
    caplog.set_level(logging.INFO)
    for attempt in range(30):
        clock[0] = attempt
        assert tasks.home()
    assert not caplog.records
    clock[0] = 60
    store.maintenance_diagnostics.flush()
    assert len(caplog.records) == 1
    assert caplog.records[0].args[2] == 30
    assert caplog.records[0].args[5] == {'rec: Home collections': {'time_budget': 30}}
    assert store.rec_proposals.get_proposals('home_prepared') == {'last-good': True}


def test_fast_phase_details_are_debug_and_slow_work_remains_visible(monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(work_diagnostics, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], thread_time=lambda: 0))
    caplog.set_level(logging.DEBUG)
    with work_diagnostics.work_span('fast', announce=True, level=logging.DEBUG):
        clock[0] = 0.1
    with work_diagnostics.work_span('slow', announce=True, level=logging.DEBUG):
        clock[0] = 2.2
    info = [record for record in caplog.records if record.levelno >= logging.INFO]
    assert len(info) == 1 and 'phase=slow' in info[0].message


def test_explicit_user_action_announcements_remain_at_info(caplog):
    caplog.set_level(logging.INFO)
    with work_diagnostics.work_span('radio: apply stream tuning', announce=True):
        pass
    assert len(caplog.records) == 2
    assert all(r.levelno == logging.INFO and 'radio: apply stream tuning' in r.message
               for r in caplog.records)


def test_bundle_failure_preserves_traceback_retry_and_last_good_pool(store, monkeypatch, caplog):
    store.rec_proposals.put_proposals('mode_bundles', {'last-good': True}, 1)

    def failed(*args):
        raise RuntimeError('forced preparation failure')

    monkeypatch.setattr(mode_surfaces, 'prepare_bundles', failed)
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: 1000,
                                      logger=logging.getLogger('test')), debounce_s=0)
    try:
        assert worker._refresh_bundles() is None
        warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].exc_info and 'retry_backoff=' in warnings[0].message
        assert 'forced preparation failure' in caplog.text
        assert store.rec_proposals.get_proposals('mode_bundles') == {'last-good': True}
    finally:
        worker.shutdown()
