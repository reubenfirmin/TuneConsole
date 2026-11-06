"""Exercise ticker admission beside repeatedly deferred serving work."""
from types import SimpleNamespace

import pytest

from tests.test_mode_refresh_worker import scheduled
from yt_playlist.core import playback_feedback
from yt_playlist.rec import candidate_refresh, mode_surfaces, rec_worker
from yt_playlist.rec.worker_queue import Job


@pytest.mark.parametrize('retry_pending', [False, True])
@pytest.mark.parametrize('pool_outcome', ['deferred', 'failure', 'success', 'policy-capacity'])
def test_ticker_admits_content_beside_pool_retries(scheduled, monkeypatch, retry_pending, pool_outcome):
    worker, targets = scheduled
    store = worker.ctx.store
    recording = store.tracks.upsert_track('cold', 'Cold content', 'Artist', None, None)
    store.tracks.set_track_genre(recording, 'House')
    store.settings.set_setting('rec_group_shadow', '0')
    assert worker.preparation.due(worker.ctx.now_fn())
    clock, calls, ticks = [100.0], [], [0]
    monkeypatch.setattr(rec_worker, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    worker.ctx.now_fn = lambda: 2000.0 + clock[0] - 100.0

    def capture(now):
        calls.append(('content', clock[0]))
        # An arrival during a content slice still gets its serving turn.
        worker.trigger_rating_update()
        return False

    def pools(*args):
        calls.append(('pools', clock[0]))
        if pool_outcome == 'policy-capacity':
            from yt_playlist.repos.policy_storage import PolicyCapacityFull
            raise PolicyCapacityFull(131000, 60000000, 1024, 500000)
        if pool_outcome == 'failure':
            raise RuntimeError('pool unavailable')
        return {'built_at': worker.ctx.now_fn()} if pool_outcome == 'success' else None

    monkeypatch.setattr(worker.preparation, 'capture', capture)
    monkeypatch.setattr(worker.preparation, 'fit', lambda now: False)
    monkeypatch.setattr(worker.tasks, 'ratings', lambda: calls.append(('ratings', clock[0])) or set())
    monkeypatch.setattr(worker, '_do_rebuild', lambda: None)
    monkeypatch.setattr(worker, 'trigger_home_refresh', lambda: None)
    monkeypatch.setattr(mode_surfaces, 'prepare_bundles', pools)
    monkeypatch.setattr(playback_feedback, 'try_drain', lambda *args: False)
    for name in ('_poll_evidence_storage', '_poll_retention', '_retry_home',
                 '_poll_choices', '_retry_measurements', '_poll_tv'):
        monkeypatch.setattr(worker, name, lambda: None)
    monkeypatch.setattr(worker.projections, 'poll', lambda: None)
    worker.jobs.retry[Job.BUNDLES].dirty = retry_pending

    class Stop:
        def set(self):
            pass

        def is_set(self):
            return False

        def wait(self, seconds):
            if seconds == 0:
                return False
            assert seconds == .5
            while targets:
                targets.pop(0)()
            ticks[0] += 1
            if ticks[0] == 125:
                # Content and ratings keep receiving turns while pool work is
                # held by its independent hourly admission. Advance both clocks
                # to exercise the next admitted round without a real-time wait.
                assert sum(kind == 'pools' for kind, _ in calls) == 1
                assert sum(kind == 'content' for kind, _ in calls) >= 10
                clock[0] += candidate_refresh.CHECK_INTERVAL_S
            clock[0] += .5
            return ticks[0] > 250

    worker._stop = Stop()
    try:
        worker._maintenance_loop()
        content = [at for kind, at in calls if kind == 'content']
        pools = [at for kind, at in calls if kind == 'pools']
        ratings = [at for kind, at in calls if kind == 'ratings']
        assert len(content) >= 20
        assert all(b - a >= 5 for a, b in zip(content, content[1:]))
        assert len(pools) >= 2
        assert all(b - a >= candidate_refresh.CHECK_INTERVAL_S for a, b in zip(pools, pools[1:]))
        assert ratings == content
        assert not targets and not worker._running and not worker.jobs
    finally:
        worker.shutdown()


def test_queued_preparation_coalesces_and_shutdown_closes_admission(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []
    monkeypatch.setattr(worker.preparation, 'due', lambda now: calls.append('due') or True)
    worker._schedule(full=False, bundles=True)
    for _ in range(10):
        worker._poll_refresh()
    assert calls == ['due']
    assert len(targets) == 1
    assert worker.jobs.has(Job.PREPARE)
    worker.shutdown()
    worker._poll_refresh()
    assert calls == ['due']
    assert not worker.jobs
    targets.pop()()
    assert not worker.work.active
