"""Retention isolation through actual worker admission, stages and cancellation."""
from collections import Counter
from types import SimpleNamespace

import pytest

from tests.test_mode_refresh_worker import scheduled, _worker
from yt_playlist.core import fao, vibe_provenance as provenance
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import group_roots, measurement, rec_worker
from yt_playlist.rec.worker_queue import Job
from yt_playlist.repos.retention import RetentionRepo


def owners():
    return ((RetentionRepo, 'maintain_managed_files', 'files', 0),
            (RetentionRepo, 'maintain_prediction_provenance', 'vibes', 0),
            (RetentionRepo, 'collect_musical_profiles', 'profiles', 0),
            (RetentionRepo, 'collect_embeddings', 'embeddings', 0),
            (RetentionRepo, 'expire_legacy_diagnostics', 'diagnostics', 0),
            (RetentionRepo, 'maintain_runtime_history', 'runtime', {'credits': 0, 'modes': 0}),
            (group_roots, 'maintain_retention', 'derived', {'pending': False}),
            (measurement, 'maintain_storage', 'policies', {'pending': False}))


@pytest.fixture
def maintenance(scheduled, monkeypatch):
    worker, targets = scheduled
    clock, calls = [100.0], []
    original_vibes = RetentionRepo.maintain_prediction_provenance
    monkeypatch.setattr(rec_worker, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    for module, method, name, result in owners():
        def healthy(*args, _name=name, _result=result, **kwargs):
            calls.append((_name, clock[0]))
            return _result
        monkeypatch.setattr(module, method, healthy)

    def run(at):
        clock[0] = at
        worker._poll_retention()
        worker._poll_retention()
        assert len(targets) == 1 and not worker.jobs.visible
        targets.pop()()
        assert not worker._running and not worker.jobs and not worker.work.active

    yield SimpleNamespace(worker=worker, targets=targets, clock=clock, calls=calls,
                          run=run, original_vibes=original_vibes)
    worker.shutdown()
    while targets:
        targets.pop()()


@pytest.mark.parametrize('domain', ['files', 'policies'])
def test_persistent_early_failure_cannot_starve_healthy_owners(maintenance, monkeypatch, domain):
    m = maintenance
    module, method, _, _ = next(owner for owner in owners() if owner[2] == domain)

    def broken(*args, **kwargs):
        m.calls.append((domain, m.clock[0]))
        raise OSError('unreadable managed directory')

    monkeypatch.setattr(module, method, broken)
    if domain == 'policies':
        def released(*args):
            m.calls.append(('derived', m.clock[0]))
            return {'pending': False, 'pools': 1}
        monkeypatch.setattr(group_roots, 'maintain_retention', released)
    at = 100
    while at <= 1000:
        m.run(at)
        next_at = m.worker.jobs.retry[Job.RETENTION].retry_at
        assert 5 <= next_at - at <= 300
        at = next_at
    counts = Counter(name for name, at in m.calls)
    assert all(counts[name] == 4 for name in
               ('files', 'vibes', 'profiles', 'embeddings', 'diagnostics', 'runtime', 'derived', 'policies')
               if name != domain)
    failures = [at for name, at in m.calls if name == domain]
    assert 4 < len(failures) < 15
    # Actual owner releases wake idle policy scans, but must not shorten an
    # existing error backoff or make a broken collector continuously retry.
    assert [b - a for a, b in zip(failures, failures[1:])] == [
        min(300, 5 * 2 ** min(index, 6)) for index in range(len(failures) - 1)]
    state = m.worker.retention.progress[
        'managed file lifecycle' if domain == 'files' else 'unused recommendation policies']
    assert state.error == 'OSError: unreadable managed directory' and state.pending
    assert state.failures == len(failures)


def test_malformed_bookmark_is_preserved_and_failed_owner_recovers(maintenance, monkeypatch):
    m = maintenance
    store = m.worker.ctx.store
    assert provenance.available(store.conn)
    bookmark = provenance.directory(store.conn) / '.history-collection.json'
    damaged = b'{damaged bookmark'
    fao.write_bytes(bookmark, damaged)
    monkeypatch.setattr(RetentionRepo, 'maintain_prediction_provenance', m.original_vibes)
    m.run(100)
    state = m.worker.retention.progress['prediction provenance retirement']
    assert state.failures == 1 and state.error.startswith('JSONDecodeError:')
    assert Counter(name for name, at in m.calls) == {
        'files': 1, 'profiles': 1, 'embeddings': 1, 'diagnostics': 1,
        'runtime': 1, 'derived': 1, 'policies': 1}
    m.run(105)
    assert state.failures == 2 and state.retry_at == 115
    assert fao.read_bytes(bookmark) == damaged
    assert len(m.calls) == 7  # Healthy idle owners keep their own deadlines.
    m.clock[0] = 114.9
    m.worker._poll_retention()
    assert not m.targets
    # Explicitly repair the controlled fixture; production does not reset it.
    fao.write_bytes(bookmark, b'{"after":0}')
    m.run(115)
    assert state.error is None and state.failures == 0 and not state.pending
    assert state.result == 0 and state.retry_at == 415
    assert len(m.calls) == 7
    assert m.worker.jobs.retry[Job.RETENTION].retry_at == 400


@pytest.mark.parametrize('domain', ['runtime', 'derived', 'policies', 'released-pools', 'released-generations'])
def test_only_pending_owner_retries_promptly(maintenance, monkeypatch, domain):
    m = maintenance
    releases = domain.startswith('released-')
    module, method, _, _ = next(owner for owner in owners() if owner[2] == ('derived' if releases else domain))
    attempts = []

    def pending(*args, **kwargs):
        attempts.append(m.clock[0])
        if domain == 'runtime':
            return {'credits': int(len(attempts) == 1), 'modes': 0}
        return {'pending': len(attempts) == 1,
                domain.removeprefix('released-'): int(releases)}

    monkeypatch.setattr(module, method, pending)
    m.run(100)
    assert m.worker.jobs.retry[Job.RETENTION].retry_at == 105
    m.clock[0] = 104.9
    m.worker._poll_retention()
    assert not m.targets
    m.run(105)
    assert attempts == [100, 105]
    assert len(m.calls) == 7 + int(releases)
    assert m.worker.jobs.retry[Job.RETENTION].retry_at == 400
    m.run(400)
    assert len(m.calls) == 14 and attempts == [100, 105]
    assert m.worker.jobs.retry[Job.RETENTION].retry_at == 405


@pytest.mark.parametrize('stop', ['exception', 'worker', 'admission'])
def test_shutdown_stops_bundle_instead_of_retrying_owner(maintenance, monkeypatch, stop):
    m = maintenance

    def close(*args):
        m.calls.append(('files', m.clock[0]))
        if stop == 'exception':
            raise WorkClosing('closing')
        if stop == 'worker':
            m.worker.shutdown()
        else:
            m.worker.work.close_admission()
        return 0

    monkeypatch.setattr(RetentionRepo, 'maintain_managed_files', close)
    m.run(100)
    assert m.calls == [('files', 100)]
    assert m.worker._stop.is_set()
    assert m.worker.retention.progress['managed file lifecycle'].failures == 0
    assert not m.worker.retention.progress['managed file lifecycle'].error
    m.worker._poll_retention()
    assert not m.targets


def test_constructor_does_not_run_retention_owners(store, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Retention ran during worker construction')

    for module, method, name, result in owners():
        monkeypatch.setattr(module, method, forbidden)
    worker = _worker(store)
    assert not worker.retention.progress and not worker.work.active
    worker.shutdown()


def test_inflight_retention_coalesces_ticker_polls_and_preserves_serving(maintenance, monkeypatch):
    m = maintenance

    def active(*args):
        m.clock[0] += 600
        for _ in range(10):
            m.worker._poll_retention()
        assert not m.worker.jobs.has(Job.RETENTION)
        m.worker.trigger_rating_update()
        return 0

    monkeypatch.setattr(RetentionRepo, 'maintain_managed_files', active)
    monkeypatch.setattr(m.worker.tasks, 'ratings',
                        lambda: m.calls.append(('ratings', m.clock[0])) or set())
    m.run(100)
    assert m.calls[-1] == ('ratings', 700)
    assert len(m.calls) == 8
    assert m.worker.jobs.retry[Job.RETENTION].retry_at == 1000


def test_priority_cleanup_failure_retains_work_and_ticker_restarts_before_ranking(maintenance, monkeypatch):
    from yt_playlist.rec import recommendation_retirement

    m = maintenance
    store = m.worker.ctx.store
    attempts = []

    def drain(owner, now):
        attempts.append(m.clock[0])
        assert owner is store
        if len(attempts) == 1:
            store._recommendation_retirement_pending = True
            # Reproduce a ticker arrival during a long cleanup attempt.
            m.worker._poll_retention()
            raise OSError('interrupted committed cleanup')
        store._recommendation_retirement_pending = False

    monkeypatch.setattr(recommendation_retirement, 'drain', drain)
    monkeypatch.setattr(m.worker.tasks, 'ratings', lambda: m.calls.append(('ratings', m.clock[0])) or set())
    monkeypatch.setattr(m.worker, '_refresh_bundles', lambda: m.calls.append(('ranking', m.clock[0])))
    m.worker.trigger_candidate_refresh()
    m.worker.trigger_rating_update()
    m.worker._poll_retention()
    m.targets.pop()()
    assert attempts == [100] and not m.calls
    assert store.activity_paused and not m.worker._running
    assert m.worker.jobs.has(Job.BUNDLES) and m.worker.jobs.has(Job.RATINGS)
    assert not m.worker.jobs.has(Job.RETENTION)
    assert m.worker.jobs.retry[Job.BUNDLES].attempted_at == float('-inf')
    m.clock[0] = 104.9
    m.worker._poll_retention()
    m.worker._poll_evidence_storage()
    m.worker.trigger_candidate_refresh()
    m.worker.trigger_rating_update()
    assert not m.targets
    m.clock[0] = 105
    m.worker._poll_retention()
    m.targets.pop()()
    assert attempts == [100, 105]
    assert m.calls[-2:] == [('ratings', 105), ('ranking', 105)]
    assert not store.activity_paused and not m.worker.jobs and not m.worker._running


def test_idle_retention_recovers_backlog_before_hourly_deadline(maintenance, monkeypatch):
    from yt_playlist.rec import candidate_refresh, recommendation_retirement

    m = maintenance
    store = m.worker.ctx.store
    candidate_refresh.checked(store, 2000)
    assert not candidate_refresh.check_due(store, 2000)
    monkeypatch.setattr(store.recommendations, 'retirement_pending', lambda: True)
    monkeypatch.setattr(recommendation_retirement, 'drain',
                        lambda *args: m.calls.append(('priority cleanup', m.clock[0])))
    m.run(100)
    assert m.calls[0] == ('priority cleanup', 100)
    assert len(m.calls) == 9
