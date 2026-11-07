"""Retention services compose repository results without opening storage.

Real checkpoints and concurrent capture/deletion fences remain covered by the
recommendation-retirement and album-feedback-outbox integration owners.
"""
from contextlib import contextmanager, nullcontext
import logging
import sqlite3
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yt_playlist.core.activity import Activity, WorkBudgetExceeded
from yt_playlist.rec import choice_evidence, group_roots, recommendation_roots
from yt_playlist.rec.retention_maintenance import Retention
from yt_playlist.repos import compact_group_publication, group_retention, recommendation_publication


class NoDatabase:
    @property
    def conn(self):
        pytest.fail('Retention service requested a raw database connection')


@pytest.fixture
def no_sqlite(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Retention service opened SQLite outside its repositories')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)


def test_maintenance_dispatch_uses_repositories_and_keeps_independent_retries(no_sqlite):
    store = NoDatabase()
    methods = ('maintain_managed_files', 'maintain_prediction_provenance',
               'collect_musical_profiles', 'collect_embeddings',
               'expire_legacy_diagnostics', 'maintain_runtime_history')
    store.retention = Mock(spec=methods)
    for method in methods[:-1]:
        getattr(store.retention, method).return_value = 0
    store.retention.maintain_managed_files.side_effect = [OSError('temporarily busy'), 2]
    store.retention.maintain_runtime_history.side_effect = [
        {'credits': 1, 'modes': 0}, {'credits': 0, 'modes': 0}]
    clock, dispatched = [100], []

    def execute(name, target, *args, **kwargs):
        dispatched.append(name)
        if name in ('derived history retirement', 'unused recommendation policies'):
            # These distinct policy services have their own repository-double
            # owners below; execute all six maintenance callbacks here.
            return {'pending': False}
        return target(*args, **kwargs)

    retention = Retention(store, lambda: 123, logging.getLogger(__name__),
                          execute=execute, monotonic=lambda: clock[0])
    assert retention.run() == 105
    files = retention.progress['managed file lifecycle']
    runtime = retention.progress['runtime history retirement']
    assert files.failures == 1 and files.pending
    assert runtime.pending and not runtime.failures
    assert len(dispatched) == 8
    for method in methods:
        getattr(store.retention, method).assert_called_once_with(123)

    clock[0] = 104
    assert retention.run() == 105 and len(dispatched) == 8
    clock[0] = 105
    assert retention.run() == 400
    assert dispatched[8:] == ['managed file lifecycle', 'runtime history retirement']
    assert files.result == 2 and not files.failures and not files.pending
    assert not runtime.pending and not runtime.failures
    assert files.retry_at == runtime.retry_at == 405


@pytest.mark.parametrize('paused_first', [False, True])
def test_gc_dispatch_uses_recovery_repository_and_resumes_daily_cadence(no_sqlite, monkeypatch, paused_first):
    from yt_playlist.library import playlist_gc_retry
    from yt_playlist.rec import discover, rec_worker

    store = NoDatabase()
    store._recommendation_retirement_pending = paused_first
    store.retention = Mock(spec=['maintain_collection_recovery', 'collection_recovery_pending'])
    store.retention.maintain_collection_recovery.return_value = 0
    store.retention.collection_recovery_pending.side_effect = [True, False]
    monkeypatch.setattr(playlist_gc_retry, 'next_retry_at', lambda owner: None)
    discoveries = []
    monkeypatch.setattr(discover, 'gc_discovery',
                        lambda ctx, now: discoveries.append(now) or {})
    clock, waits = [0], []
    monkeypatch.setattr(rec_worker, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            clock[0] += seconds
            if seconds == 5:
                store._recommendation_retirement_pending = False
            return len(waits) == 3 + int(paused_first)

        def is_set(self):
            return False

    worker = rec_worker.RecWorker.__new__(rec_worker.RecWorker)
    worker.ctx = SimpleNamespace(store=store, client_provider=lambda: {},
                                now_fn=lambda: clock[0], logger=logging.getLogger(__name__))
    worker.work, worker._stop = Activity(), Stop()
    worker.gc_initial_s, worker.gc_tick_s = 0, 86400
    worker._gc_loop()
    assert store.retention.maintain_collection_recovery.call_args_list == [(), ()]
    assert store.retention.collection_recovery_pending.call_args_list == [(), ()]
    assert waits == ([0, 5, 60, 86340] if paused_first else [0, 60, 86340])
    assert discoveries == [5 if paused_first else 0]
    assert not worker.work.active


def test_recommendation_retention_checkpoints_each_bounded_repository_turn(
        no_sqlite, monkeypatch, tmp_path):
    store = NoDatabase()
    store._compact_group_files = True
    store._db_path = tmp_path / 'never-opened.sqlite'
    store._lock = RLock()
    store.recommendations = Mock(spec=['checkpoint_retirement', 'cleanup_generations', 'cleanup_pools'])
    store.taste_groups = Mock(spec=['cleanup_builds', 'retire_history'])
    store.taste_groups.cleanup_builds.return_value = False
    store.taste_groups.retire_history.return_value = False
    store.group_evidence = Mock(spec=[
        'collect_unpublished_mixes', 'output_collector', 'collect_output_files'])
    store.group_evidence.collect_output_files.return_value = SimpleNamespace(
        files=0, byte_size=0, checked=1, blocked=False, pending=False)
    events = []
    generations = iter([SimpleNamespace(rows=3, generations=1, pools=0)])

    def generation_turn(*, checkpoint):
        assert checkpoint is False
        events.append('generations')
        if progress := next(generations, None):
            return progress
        raise WorkBudgetExceeded('This committed prefix exhausted its turn')

    def pool_turn(*, checkpoint):
        assert checkpoint is False
        events.append('pools')
        return None

    store.recommendations.checkpoint_retirement.side_effect = lambda: events.append('checkpoint')
    monkeypatch.setattr(recommendation_roots, 'enabled', lambda value: True)
    store.recommendations.cleanup_generations.side_effect = generation_turn
    store.recommendations.cleanup_pools.side_effect = pool_turn
    # OS lifetime-file reaping is a separate integration obligation.
    monkeypatch.setattr(group_roots, 'reap_pending', lambda value: None)

    result = group_roots.maintain_retention(store)

    assert result['pending'] and result['recommendation_rows'] == 3
    assert result['generations'] == 1 and result['pools'] == 0
    assert events == ['generations', 'generations', 'checkpoint', 'pools', 'checkpoint']
    assert store.recommendations.checkpoint_retirement.call_count == 2
    assert not store._retention_lock.locked()
    assert not store._db_path.exists()


def test_evidence_retention_enters_repository_protection_at_final_deletion(no_sqlite, monkeypatch):
    from yt_playlist.rec import home_cache

    store = NoDatabase()
    store.choice_capture = Mock(spec=['artifacts', 'opportunities'])
    store.choice_capture.artifacts.return_value = {'inventory-artifact'}
    store.choice_capture.opportunities.return_value = {'inventory-opportunity'}
    store.settings = Mock(spec=['get_setting'])
    store.settings.get_setting.side_effect = lambda key: (
        'retention' if key == choice_evidence.STORAGE_PHASE else None)
    store.rec_proposals = Mock(get_proposals=Mock(return_value={}))
    cache_reads = []

    def cached(owner, surface):
        assert owner is store and surface in home_cache.SURFACES
        cache_reads.append(surface)
        return {'choice_context': {'artifact': surface + '-artifact'}}

    monkeypatch.setattr(home_cache, 'read', cached)
    store.radio_checkpoints = Mock(load=Mock(return_value=({}, 0)))
    store.read_view = lambda: nullcontext(store)
    store.model_selection = Mock(
        retained_pair_artifacts=Mock(return_value=([], [])),
        selection_runs=Mock(return_value=[]))
    ledger = Mock(byte_budget=1024)
    store.rec_ledger = Mock(evidence=Mock(return_value=ledger))
    events = []

    @contextmanager
    def protection(capture):
        assert capture is store.choice_capture
        events.append('protected')
        try:
            yield {'late-artifact'}, {'late-opportunity'}
        finally:
            events.append('released')

    store.album_feedback = Mock(protection=Mock(side_effect=protection))

    def maintain(cutoff, *, pending_protection, **options):
        assert not events
        assert options['protected_artifacts'] == {'inventory-artifact'} | {
            surface + '-artifact' for surface in home_cache.SURFACES}
        assert options['protected_opportunities'] == {'inventory-opportunity'}
        with pending_protection() as (artifacts, opportunities):
            assert artifacts == {'late-artifact'} and opportunities == {'late-opportunity'}
            events.append('final pin check and deletion')
        return {'retained': 1}

    ledger.maintain.side_effect = maintain

    assert choice_evidence._maintain_storage(store, 1_800_000_000) == {'retained': 1}
    assert events == ['protected', 'final pin check and deletion', 'released']
    assert cache_reads == ['home_prepared', 'home_tuned', 'home_choice']
    store.album_feedback.protection.assert_called_once_with(store.choice_capture)
