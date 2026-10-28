from tests.group_support import publish, recordings
from tests.test_mode_refresh_worker import scheduled
from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import group_serving
from yt_playlist.rec.worker_queue import Job


def test_shadow_groups_run_after_home_and_do_not_show_a_model_refresh(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []
    monkeypatch.setattr(worker.tasks, 'home', lambda: calls.append('home') or False)
    monkeypatch.setattr(worker.tasks, 'stories', lambda: False)
    monkeypatch.setattr(worker, 'trigger_tv_refresh', lambda: False)
    monkeypatch.setattr(worker.preparation.groups, 'step', lambda now: calls.append('groups'))
    monkeypatch.setattr(worker.ctx.store.taste_groups, 'head', lambda: {'generation': 'prepared'})
    monkeypatch.setattr(group_serving, 'prepare', lambda *args: calls.append('preview') or {})
    worker._schedule(full=False, groups=True)
    assert not worker.busy
    worker.trigger_home_refresh(force=True)
    targets.pop()()
    assert calls == ['home', 'groups', 'preview']
    assert not worker.busy and not worker.jobs


def test_arriving_home_work_interrupts_shadow_without_losing_the_retry(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []

    def grouping(now):
        calls.append('groups')
        worker.trigger_home_refresh(force=True)
        check_stopping()

    monkeypatch.setattr(worker.preparation.groups, 'step', grouping)
    monkeypatch.setattr(worker.tasks, 'home', lambda: calls.append('home') or False)
    monkeypatch.setattr(worker.tasks, 'stories', lambda: False)
    monkeypatch.setattr(worker, 'trigger_tv_refresh', lambda: False)
    monkeypatch.setattr(group_serving, 'prepare', lambda *args: calls.append('preview'))
    worker._schedule(full=False, groups=True)
    targets.pop()()
    assert calls == ['groups', 'home']
    assert worker.jobs.retry[Job.GROUPS].dirty
    assert not worker.busy and not worker.jobs


def test_background_capture_arrivals_do_not_cancel_graph_progress(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []

    def grouping(now):
        calls.append('groups')
        worker._schedule(full=False, prepare=True, bundles=True)
        check_stopping()

    monkeypatch.setattr(worker.preparation.groups, 'step', grouping)
    monkeypatch.setattr(worker.ctx.store.taste_groups, 'head', lambda: {'generation': 'prepared'})
    monkeypatch.setattr(group_serving, 'prepare', lambda *args: calls.append('preview') or {})
    monkeypatch.setattr(worker, '_prepare_content', lambda: calls.append('capture'))
    monkeypatch.setattr(worker, '_refresh_bundles', lambda: calls.append('bundles'))
    worker._schedule(full=False, groups=True)
    targets.pop()()
    assert calls == ['groups', 'preview', 'capture', 'bundles']
    assert not worker.jobs.retry[Job.GROUPS].dirty


def test_already_pending_background_work_does_not_starve_a_graph_slice(scheduled, monkeypatch):
    worker, _targets = scheduled
    calls = []
    monkeypatch.setattr(worker.preparation.groups, 'step', lambda now: calls.append('groups'))
    monkeypatch.setattr(worker.ctx.store.taste_groups, 'head', lambda: {'generation': 'prepared'})
    monkeypatch.setattr(group_serving, 'prepare', lambda *args: calls.append('preview') or {})
    worker.jobs.merge(Job.PREPARE)
    worker.jobs.merge(Job.BUNDLES)
    worker._prepare_groups()
    assert calls == ['groups', 'preview']
    assert worker.jobs.has(Job.PREPARE) and worker.jobs.has(Job.BUNDLES)


def test_poll_retries_missing_preview_after_restart_without_new_graph_inputs(scheduled, monkeypatch):
    worker, _targets = scheduled
    publish(worker.ctx.store, recordings([('House', 3, .1)]))
    assert not worker.preparation.groups.due(2000)
    calls = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: calls.append(kwargs))
    worker._poll_refresh()
    assert {'full': False, 'groups': True} in calls


def test_failed_preview_keeps_retry_and_reports_waiting(scheduled, monkeypatch):
    from yt_playlist.rec import group_preparation
    worker, _targets = scheduled
    monkeypatch.setattr(worker.preparation.groups, 'step', lambda now: False)
    monkeypatch.setattr(worker.ctx.store.taste_groups, 'head', lambda: {'generation': 'prepared'})
    monkeypatch.setattr(group_serving, 'prepare', lambda *args: None)
    group_preparation.report(worker.ctx.store, 'ready', 1000)
    worker._prepare_groups()
    assert worker.jobs.retry[Job.GROUPS].dirty
    assert group_preparation.status(worker.ctx.store)['reason'] == 'preview-pending'


def test_poll_retries_persisted_failure_after_restart_even_with_a_complete_preview(scheduled, monkeypatch):
    from yt_playlist.rec import group_preparation
    worker, _targets = scheduled
    publish(worker.ctx.store, recordings([('House', 3, .1)]))
    assert group_serving.prepare(worker.ctx.store, 1100)
    group_preparation.report(worker.ctx.store, 'failed', 1200, reason='preparation-failed')
    calls = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: calls.append(kwargs))
    worker._poll_refresh()
    assert {'full': False, 'groups': True} in calls


def test_poll_retires_disabled_preview_wait_even_during_retry_backoff(scheduled, monkeypatch):
    worker, _targets = scheduled
    store = worker.ctx.store
    store.settings.set_setting('rec_group_shadow', '0')
    store.maintenance_diagnostics.record('deferred', phase=group_serving.PHASE)
    worker.jobs.retry[Job.GROUPS].retry_at = float('inf')
    calls = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: calls.append(kwargs))
    worker._poll_refresh()
    worker._poll_refresh()
    assert not store.maintenance_diagnostics.waiting
    assert store.maintenance_diagnostics.reasons[group_serving.PHASE, 'disabled'] == 1
    assert {'full': False, 'groups': True} not in calls
