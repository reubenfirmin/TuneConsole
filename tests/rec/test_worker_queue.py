"""Coalescing, ordering, retry and cancellation across scheduler handoffs."""
import logging
from types import SimpleNamespace

import pytest

from tests.test_mode_refresh_worker import scheduled
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import rec_worker
from yt_playlist.rec.maintenance import Projections
from yt_playlist.rec.worker_queue import Job, PendingJobs, Retry


def test_detached_batches_preserve_new_arrivals_and_bound_artwork():
    queue = PendingJobs()
    for _ in range(3):
        queue.merge(Job.FULL)
        queue.merge(Job.MODES)
        queue.merge(Job.CONTENT, {"one", "two"})
    queue.merge(Job.ARTWORK, range(50))
    queue.merge(Job.HOME)
    batch = queue.drain()
    assert list(batch) == [Job.FULL, Job.MODES, Job.CONTENT, Job.HOME, Job.ARTWORK, Job.STORIES]
    assert batch[Job.ARTWORK] == [0, 1, 2]
    assert batch[Job.CONTENT] == {"one", "two"}
    queue.merge(Job.CONTENT, {"three"})
    assert queue.drain()[Job.CONTENT] == {"three"}
    assert batch[Job.CONTENT] == {"one", "two"}
    remaining = []
    while queue:
        remaining.extend(queue.drain()[Job.ARTWORK])
    assert remaining == list(range(6, 40))


def test_measurement_bound_counts_offers_and_excludes_analytics_from_visible_busy():
    queue = PendingJobs()
    for _ in range(8):
        assert queue.merge(Job.MEASUREMENT, SimpleNamespace(work_units=8))
    assert not queue.merge(Job.MEASUREMENT, SimpleNamespace(work_units=1))
    assert queue and not queue.visible
    assert len(queue.take_measurements()) == 8 and not queue


def test_evidence_maintenance_runs_without_home_requests_and_is_coalesced(scheduled, monkeypatch):
    from yt_playlist.rec import choice_evidence
    worker, targets = scheduled
    calls, now = [], [100]
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(choice_evidence, 'maintain_storage', lambda *args: calls.append(True))
    worker._poll_evidence_storage()
    worker._poll_evidence_storage()
    assert len(targets) == 1 and not worker.jobs.visible
    targets.pop()()
    assert calls == [True] and not worker.busy
    now[0] = 399
    worker._poll_evidence_storage()
    assert not targets
    now[0] = 400
    worker._poll_evidence_storage()
    targets.pop()()
    assert calls == [True, True]


def test_measurement_batches_preserve_fifo_and_leave_other_jobs_admitted():
    queue = PendingJobs()
    offers = [SimpleNamespace(work_units=units) for units in (3, 4, 2, 1)]
    for offer in offers:
        assert queue.merge(Job.MEASUREMENT, offer)
    queue.merge(Job.RATINGS)
    assert queue.take_measurements(limit=8) == offers[:2]
    assert list(queue.measurements) == offers[2:]
    assert queue.drain() == {Job.RATINGS: True}
    assert queue.take_measurements(limit=8) == offers[2:]
    assert not queue


def test_inflight_capture_slots_stay_reserved_and_retries_precede_later_arrivals():
    queue = PendingJobs()
    offers = [SimpleNamespace(work_units=1, identity=index) for index in range(64)]
    for offer in offers:
        assert queue.merge(Job.MEASUREMENT, offer)
    batch = queue.take_measurements(limit=8)
    extra = SimpleNamespace(work_units=1, identity='extra')
    assert not queue.merge(Job.MEASUREMENT, extra)
    queue.finish_measurements(batch, retry=batch[2:])
    assert list(queue.measurements) == offers[2:]
    assert queue.merge(Job.MEASUREMENT, extra)
    assert queue.merge(Job.MEASUREMENT, SimpleNamespace(work_units=1))
    assert not queue.merge(Job.MEASUREMENT, SimpleNamespace(work_units=1))
    next_batch = queue.take_measurements(limit=8)
    assert next_batch == offers[2:10]
    queue.finish_measurements(next_batch)
    assert queue.merge(Job.MEASUREMENT, SimpleNamespace(work_units=8))
    assert not queue.merge(Job.MEASUREMENT, SimpleNamespace(work_units=1))


def test_worker_services_ratings_between_measurement_batches(scheduled, monkeypatch):
    worker, targets = scheduled
    calls, closed = [], set()
    for index in range(19):
        assert worker.enqueue_measurement(SimpleNamespace(surface='home',
            run=lambda index=index: calls.append(index), close=lambda index=index: closed.add(index)))
    monkeypatch.setattr(worker.tasks, 'ratings', lambda: calls.append('ratings') or set())
    worker.trigger_rating_update()
    assert len(targets) == 1
    targets.pop()()
    assert calls == [*range(8), 'ratings', *range(8, 19)]
    assert closed == set(range(19)) and not worker.jobs and not worker._running


@pytest.mark.parametrize(("base", "cap", "exponent_cap", "expected"), [
    (2, 30, 4, [2, 4, 8, 16, 30, 30, 30]),
    (5, 300, 6, [5, 10, 20, 40, 80, 160, 300]),
])
def test_retry_windows_preserve_model_and_projection_backoff(base, cap, exponent_cap, expected):
    state = Retry()
    actual = [state.failed(100, base=base, cap=cap, exponent_cap=exponent_cap) for _ in expected]
    assert actual == expected and state.retry_at == 100 + expected[-1] and state.dirty
    state.succeeded()
    assert state.failures == 0 and state.retry_at == 0 and not state.dirty


def test_direct_rebuild_hands_all_arrivals_to_one_serial_continuation(scheduled, monkeypatch):
    worker, targets = scheduled
    calls, closed = [], set()
    offer = SimpleNamespace(surface="home", run=lambda: calls.append("measurement"), close=lambda: closed.add(1))

    def rebuild():
        calls.append("full")
        worker.trigger_rating_update()
        worker.trigger_home_refresh(force=True)
        worker.trigger_artist_images([4, 3, 2, 1])
        assert worker.enqueue_measurement(offer)

    monkeypatch.setattr(worker, "_do_rebuild", rebuild)
    monkeypatch.setattr(worker.tasks, "ratings", lambda: calls.append("ratings") or set())
    monkeypatch.setattr(worker.tasks, "home", lambda: calls.append("home") or False)
    monkeypatch.setattr(worker.tasks, "artwork", lambda ids: calls.append(tuple(ids)))
    monkeypatch.setattr(worker.tasks, "stories", lambda: calls.append("stories") or False)
    worker.rebuild()
    assert len(targets) == 1
    targets.pop()()
    assert calls == ["full", "measurement", "ratings", "home", (1, 2, 3), "stories", (4,)]
    assert closed == {1} and not worker.busy and not worker.work.active and not worker.jobs


def test_arrival_during_a_job_is_drained_without_starting_another_worker(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []

    def ratings():
        calls.append("ratings")
        worker.trigger_home_refresh(force=True)
        assert worker.busy
        return set()

    monkeypatch.setattr(worker.tasks, "ratings", ratings)
    monkeypatch.setattr(worker.tasks, "home", lambda: calls.append("home") or False)
    monkeypatch.setattr(worker.tasks, "stories", lambda: calls.append("stories") or False)
    worker.trigger_rating_update()
    assert len(targets) == 1
    targets.pop()()
    assert calls == ["ratings", "home", "stories"] and not targets and not worker.busy


def test_model_backoff_preserves_retry_without_delaying_ratings(scheduled, monkeypatch):
    from yt_playlist.rec import candidate_refresh
    worker, targets = scheduled
    now, calls = [100], []
    monkeypatch.setattr(rec_worker.time, "monotonic", lambda: now[0])
    # Missing/expired candidates have an independent recovery job. This case
    # isolates the model failure clock; candidate recovery is covered separately.
    monkeypatch.setattr(worker.ctx.store.rec_proposals, 'has_proposals', lambda *_: True)
    monkeypatch.setattr(candidate_refresh, 'due', lambda *_: False)

    def fail():
        worker.jobs.retry[Job.FULL].started(now[0])
        calls.append('full')
        worker.trigger()
        worker.trigger_rating_update()
        raise RuntimeError("temporary model failure")

    monkeypatch.setattr(worker, "_do_rebuild", fail)
    monkeypatch.setattr(worker.tasks, 'ratings', lambda: calls.append('rating') or set())
    worker.trigger()
    targets.pop()()
    assert calls == ['full', 'rating']
    assert worker.jobs.retry[Job.FULL].dirty
    worker._poll_refresh()
    assert not targets
    now[0] = 159
    worker._poll_refresh()
    assert not targets
    now[0] = 160
    worker._poll_refresh()
    targets.pop()()
    assert calls == ['full', 'rating', 'full', 'rating']
    assert worker.jobs.retry[Job.FULL].retry_at == 220
    worker.shutdown()
    assert not worker.jobs


def test_expiry_refreshes_pools_without_refitting_models(scheduled, monkeypatch):
    from yt_playlist.rec import candidate_refresh
    worker, targets = scheduled
    calls = []
    monkeypatch.setattr(worker.ctx.store.rec_refresh, 'has_pending_changes', lambda: False)
    monkeypatch.setattr(candidate_refresh, 'due', lambda *args: True)
    monkeypatch.setattr(worker, '_do_rebuild', lambda: calls.append('full'))
    monkeypatch.setattr(worker, '_refresh_bundles', lambda: calls.append('bundles'))
    worker._poll_refresh()
    targets.pop()()
    assert calls == ['bundles']


@pytest.mark.parametrize('bundles_pending', [False, True])
@pytest.mark.parametrize('arrives_during_refresh', [False, True])
def test_preparation_coalesces_refresh_without_losing_later_arrivals(
        scheduled, monkeypatch, bundles_pending, arrives_during_refresh):
    worker, targets = scheduled
    calls = []
    monkeypatch.setattr(worker.preparation, 'capture', lambda now: calls.append('capture') or True)
    monkeypatch.setattr(worker.preparation, 'fit', lambda now: False)

    def refresh():
        calls.append('bundles')
        if arrives_during_refresh and calls.count('bundles') == 1:
            worker.trigger_candidate_refresh()

    monkeypatch.setattr(worker, '_refresh_bundles', refresh)
    worker._schedule(full=False, prepare=True, bundles=bundles_pending)
    assert len(targets) == 1
    targets.pop()()
    assert calls == ['capture', 'bundles'] + (['bundles'] if arrives_during_refresh else [])
    assert not targets and not worker.jobs and not worker.busy


def test_prepared_refresh_survives_a_failed_full_rebuild(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []
    monkeypatch.setattr(worker.preparation, 'capture', lambda now: calls.append('capture') or True)
    monkeypatch.setattr(worker.preparation, 'fit', lambda now: False)
    monkeypatch.setattr(worker.preparation, 'full_due', lambda now: True)

    def fail():
        calls.append('full')
        raise RuntimeError('Model fit failed after preparation')

    monkeypatch.setattr(worker, '_do_rebuild', fail)
    monkeypatch.setattr(worker, '_refresh_bundles', lambda: calls.append('bundles'))
    worker._schedule(full=True, prepare=True, bundles=True)
    targets.pop()()
    assert calls == ['capture', 'full', 'bundles']
    assert worker.jobs.retry[Job.FULL].dirty
    assert not targets and not worker.jobs and not worker.busy


def test_deferred_builds_back_off_without_becoming_failures():
    state = Retry()
    deadlines = []
    for _ in range(6):
        state.deferred(100)
        deadlines.append(state.retry_at)
    assert deadlines == [105, 110, 120, 140, 160, 160]
    assert state.failures == 0 and state.dirty
    state.succeeded()
    assert state.deferrals == 0 and not state.dirty


@pytest.mark.parametrize('outcome', ['succeeded', 'deferred', 'failed'])
def test_learning_admission_budget_survives_every_outcome(outcome):
    state = Retry(minimum_interval=60)
    state.started(100)
    if outcome == 'succeeded':
        state.succeeded()
    else:
        getattr(state, outcome)(115)
    assert state.retry_at == 160
    assert state.dirty is (outcome != 'succeeded')


def test_successful_pool_build_coalesces_next_request_but_keeps_rating_work(scheduled, monkeypatch):
    from yt_playlist.rec import mode_surfaces
    worker, targets = scheduled
    now, calls = [100], []
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(mode_surfaces, 'prepare_bundles', lambda *args: calls.append('pools') or {})
    monkeypatch.setattr(worker, 'trigger_home_refresh', lambda **kwargs: None)
    monkeypatch.setattr(worker.tasks, 'ratings', lambda: calls.append('ratings') or set())
    worker._refresh_bundles()
    now[0] = 115
    worker._schedule(full=False, bundles=True, ratings=True)
    targets.pop()()
    assert calls == ['pools', 'ratings']
    assert worker.jobs.retry[Job.BUNDLES].dirty
    now[0] = 160
    worker._retry_bundles()
    targets.pop()()
    assert calls == ['pools', 'ratings', 'pools']
    assert not worker.jobs.retry[Job.BUNDLES].dirty


def test_projection_admission_can_be_used_without_a_worker_and_releases_rejected_jobs(store,monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_LISTENING_CHARTS','1')
    scheduled_kinds = []
    accepting = [False]

    def schedule(kinds):
        scheduled_kinds.append(dict(kinds))
        return accepting[0]

    projections = Projections(store, lambda: 100, logging.getLogger(__name__), accepting=lambda: True,
                              schedule=schedule, execute=lambda *args: {})
    projections.poll()
    assert scheduled_kinds == [{"trends": True, "cleanup": True, "analytics": True}] and not projections.active
    accepting[0] = True
    projections.poll()
    assert projections.active == {"trends", "cleanup", "analytics"}
    projections.run("trends")
    assert projections.active == {"cleanup", "analytics"} and projections.completed["trends"][-1]


def test_shutdown_cancels_every_job_and_reader_and_rejects_direct_rebuild(scheduled, monkeypatch):
    worker, _ = scheduled
    closed, calls = [], []
    with worker._lock:
        for job in Job:
            payload = (SimpleNamespace(close=lambda: closed.append(True)) if job == Job.MEASUREMENT
                       else {1} if job in {Job.ARTWORK, Job.CONTENT} else True)
            worker.jobs.merge(job, payload)
            worker.jobs.retry[job].dirty = True
    monkeypatch.setattr(worker, "_do_rebuild", lambda: calls.append("rebuild"))
    worker.shutdown()
    worker.rebuild()
    worker.trigger()
    worker.projections.poll()
    assert closed == [True] and not calls and not worker.jobs and not worker.busy
    assert not any(state.dirty for state in worker.jobs.retry.values())


def test_application_cancellation_releases_jobs_arriving_during_execution(scheduled, monkeypatch):
    worker, targets = scheduled
    closed, calls = [], []
    offer = SimpleNamespace(surface="home", run=lambda: calls.append("measurement"),
                            close=lambda: closed.append(True))

    def ratings():
        worker.trigger_home_refresh(force=True)
        assert worker.enqueue_measurement(offer)
        worker.work.close_admission()
        raise WorkClosing("application shutdown")

    monkeypatch.setattr(worker.tasks, "ratings", ratings)
    monkeypatch.setattr(worker.tasks, "home", lambda: calls.append("home"))
    worker.trigger_rating_update()
    targets.pop()()

    assert closed == [True] and not calls
    assert not worker.jobs and not worker.busy and not worker.work.active
    assert not worker._running
    assert not any(state.dirty for state in worker.jobs.retry.values())
