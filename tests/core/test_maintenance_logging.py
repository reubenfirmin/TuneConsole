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


def test_job_progress_keeps_original_wait_until_explicit_completion(caplog):
    clock = [0.]
    diagnostics = work_diagnostics.MaintenanceDiagnostics(clock=lambda: clock[0])
    caplog.set_level(logging.INFO)
    with work_diagnostics.work_span('outer worker'):
        with work_diagnostics.maintenance_job('requested job'):
            diagnostics.record('deferred', reasons=('missing',))
            clock[0] = 10.
            diagnostics.record('published')
            clock[0] = 20.
            diagnostics.record('deferred', reasons=('budget',))
            assert diagnostics.waiting == {'requested job': 0.}
            clock[0] = 60.
            diagnostics.flush()
            summary = caplog.records[-1]
            assert summary.args[1] == 1
            assert summary.args[-1] == {'requested job': 60.}
            diagnostics.record('completed')
            assert not diagnostics.waiting
        diagnostics.record('deferred')
        diagnostics.record('published')
        assert not diagnostics.waiting
    assert diagnostics.phases['outer worker', 'published'] == 1


def test_disabled_job_is_retired_once_and_context_is_restored_after_failure():
    diagnostics = work_diagnostics.MaintenanceDiagnostics(clock=lambda: 0.)
    diagnostics.record('deferred', phase='disabled job')
    diagnostics.record('deferred', phase='pending job')
    diagnostics.resolve_wait('disabled job', 'disabled')
    diagnostics.resolve_wait('disabled job', 'disabled')
    assert diagnostics.waiting == {'pending job': 0.}
    assert diagnostics.phases['disabled job', 'obsolete'] == 1
    assert diagnostics.reasons['disabled job', 'disabled'] == 1
    with work_diagnostics.work_span('worker'):
        with pytest.raises(RuntimeError), work_diagnostics.maintenance_job('failed job'):
            raise RuntimeError('interrupted')
        diagnostics.record('published')
    assert diagnostics.phases['worker', 'published'] == 1


def test_home_revisit_partial_publications_preserve_wait_then_complete(store, monkeypatch):
    clock = [0.]
    store.maintenance_diagnostics = work_diagnostics.MaintenanceDiagnostics(clock=lambda: clock[0])
    attempts = []
    fail = [True]

    def albums(*args, **kwargs):
        attempts.append('albums')
        if fail[0]:
            raise WorkBudgetExceeded('unfinished album summary')
        return ['album']

    monkeypatch.setattr(home_prepared.surfaces, 'rediscover_playlists', lambda *a, **kw: ['playlist'])
    monkeypatch.setattr(home_prepared.surfaces, 'rediscover_albums', albums)
    monkeypatch.setattr(home_prepared.surfaces, 'rediscover_tracks', lambda *a, **kw: ['track'])
    with work_diagnostics.work_span('rec: different worker label'):
        home_prepared.prepare_revisit(store, 1000)
        assert store.rec_proposals.get_proposals('home_revisit')['rediscover'] == ['playlist']
        assert store.maintenance_diagnostics.waiting == {home_prepared.REVISIT_PHASE: 0.}
        clock[0] = 20.
        home_prepared.prepare_revisit(store, 1060)
        assert store.rec_proposals.get_proposals('home_revisit')['rediscover_tracks'] == ['track']
        assert store.maintenance_diagnostics.waiting == {home_prepared.REVISIT_PHASE: 0.}
        fail[0] = False
        home_prepared.prepare_revisit(store, 1120)
    assert len(attempts) == 3
    assert not store.maintenance_diagnostics.waiting
    assert store.maintenance_diagnostics.phases[home_prepared.REVISIT_PHASE, 'completed'] == 1


def test_home_revisit_stale_slice_cannot_be_hidden_by_other_publications(store, monkeypatch):
    store.settings.set_setting('revisit-input', 'old')
    phase = home_prepared.REVISIT_PHASE
    store.maintenance_diagnostics.record('deferred', phase=phase)
    start = store.maintenance_diagnostics.waiting[phase]

    def stale(reader, *args, **kwargs):
        reader.settings.get_setting('revisit-input')
        store.settings.set_setting('revisit-input', 'changed')
        return ['stale']

    monkeypatch.setattr(home_prepared.surfaces, 'rediscover_playlists', stale)
    monkeypatch.setattr(home_prepared.surfaces, 'rediscover_albums', lambda *a, **kw: ['album'])
    monkeypatch.setattr(home_prepared.surfaces, 'rediscover_tracks', lambda *a, **kw: ['track'])
    home_prepared.prepare_revisit(store, 1000)
    assert store.maintenance_diagnostics.waiting == {phase: start}
    cached = store.rec_proposals.get_proposals('home_revisit')
    assert 'rediscover' not in cached and cached['rediscover_tracks'] == ['track']
    monkeypatch.setattr(home_prepared.surfaces, 'rediscover_playlists', lambda *a, **kw: ['current'])
    home_prepared.prepare_revisit(store, 1060)
    assert not store.maintenance_diagnostics.waiting
