"""TV is prepared automatically, coalesces and retains its own network backoff."""
import pytest

from tests.test_mode_refresh_worker import scheduled
from yt_playlist.rec import rec_worker, tv
from yt_playlist.rec.worker_queue import Job


@pytest.mark.parametrize('available,interval', [(0, 5), (tv.QUEUE_SIZE, 60)])
def test_tv_preparation_runs_without_opening_home_and_coalesces(scheduled, monkeypatch, available, interval):
    worker, targets = scheduled
    calls, now = [], [100]
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(worker.tasks, 'tv', lambda: calls.append('tv') or True)
    monkeypatch.setattr(tv, 'queue', lambda *_: [{}] * available)
    worker._poll_tv()
    worker.trigger_tv_refresh()
    worker.trigger_tv_refresh()
    assert len(targets) == 1 and not worker.busy
    targets.pop()()
    assert calls == ['tv'] and worker.jobs.retry[Job.TV].dirty
    worker._poll_tv()
    assert not targets
    now[0] += interval - 1
    worker._poll_tv()
    assert not targets
    now[0] += 1
    worker._poll_tv()
    targets.pop()()
    assert calls == ['tv', 'tv']


def test_tv_refill_is_queued_while_other_recommendation_work_is_active(scheduled):
    worker, targets = scheduled
    worker._running = True
    worker._model_active = True
    assert worker.busy
    worker._poll_tv()
    assert worker.jobs.has(Job.TV)
    assert not targets  # the existing worker owns execution


def test_tv_failure_keeps_pool_and_does_not_block_other_jobs(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []
    previous = {'schema': tv.SCHEMA, 'key': 'previous', 'at': 1, 'items': [], 'attempted': []}
    worker.ctx.store.rec_proposals.put_proposals(tv.SURFACE, previous, 1)
    def fail():
        calls.append('tv')
        raise RuntimeError('provider unavailable')
    monkeypatch.setattr(worker.tasks, 'tv', fail)
    monkeypatch.setattr(worker.tasks, 'ratings', lambda: calls.append('ratings') or set())
    worker.trigger_tv_refresh()
    worker.trigger_rating_update()
    targets.pop()()
    assert calls == ['ratings', 'tv']
    assert worker.jobs.retry[Job.TV].dirty
    assert tv.cached(worker.ctx.store) == previous
    assert not worker.busy and not worker._running


def test_exhausted_searches_retry_deeper_candidates_without_waiting_for_cache_expiry(scheduled, monkeypatch):
    from tests.test_tv import Videos, row
    from yt_playlist.rec import tv_artwork

    worker, targets = scheduled
    now = [1000]
    worker.ctx.now_fn = lambda: now[0]
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: now[0])
    candidates = [{'key': f'source-{i}', 'title': f'Prepared {i}', 'artist': 'Artist', 'group': 'liked'}
                  for i in range(6)]
    offsets = []
    def seeds(store, at, offset=0):
        offsets.append(offset)
        return candidates[:3] if not offset else candidates[3:]
    monkeypatch.setattr(tv, 'seed_tracks', seeds)
    monkeypatch.setattr(tv_artwork, 'prepare', lambda *args: False)
    client = Videos([row(3, title='Prepared 3 Live session')])
    iid = worker.ctx.store.identities.upsert_identity('main', 'cred', None, True)
    worker.ctx.client_provider = lambda: {iid: client}
    worker._poll_tv()
    targets.pop()()
    assert not tv.queue(worker.ctx.store, now[0])
    assert tv.cached(worker.ctx.store)['state'] == 'exhausted'
    assert not worker.jobs.retry[Job.TV].dirty
    now[0] += tv.SEARCH_RETRY_S - 1
    worker._poll_tv()
    assert not targets
    now[0] += 1
    worker._poll_tv()
    targets.pop()()
    assert offsets[-1] == tv.MAX_POOL // len(tv.GROUPS)
    assert tv.queue(worker.ctx.store, now[0])[0]['video_id'] == 'visual00003'
