"""Explicit discovery jobs must not report an uncommitted ranking as complete."""
import logging
from types import SimpleNamespace

import pytest

from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.rec import fresh_pool
from yt_playlist.web import jobs


@pytest.mark.parametrize('result,waiting,failed', [
    (None, True, False), ({}, False, False),
    (ActionInputError('fresh_preferences_changed'), False, True),
], ids=['deferred', 'published', 'changed-preferences'])
def test_fresh_job_defers_to_worker_without_repeating_discovery(
        monkeypatch, result, waiting, failed):
    calls, discovered, refreshed, queued = [], [], [], []
    monkeypatch.setattr(fresh_pool, 'populate', lambda *args, **kwargs: discovered.append(kwargs))
    def prepare(*args):
        calls.append(args)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(fresh_pool, 'prepare', prepare)
    monkeypatch.setattr(jobs, 'activity_for', lambda ctx: SimpleNamespace(start_thread=lambda run: run()))
    heads = []
    def head(surface):
        heads.append(surface)
        return {'id': 'previous-publication'}
    ctx = SimpleNamespace(store=SimpleNamespace(recommendations=SimpleNamespace(head=head)),
        now_fn=lambda: 1, jobs=jobs.SyncJobs(),
        rec_worker=SimpleNamespace(trigger_home_refresh=lambda **kwargs: refreshed.append(kwargs),
                                  trigger_candidate_refresh=lambda: queued.append(True)),
        logger=logging.getLogger(__name__))
    parameters = {'include_genres': ['jazz'], 'avoid_genres': []}
    job = jobs.start_fresh_pool(ctx, parameters)
    assert job.done is (not waiting)
    assert len(discovered) == 1 and calls == [(ctx.store, 1)]
    assert heads == ['fresh']
    assert job.result == {'generation': 'previous-publication', 'requested_at': 1}
    assert bool(job.error) == failed
    assert refreshed == ([] if failed else [{'force': True}])
    assert queued == ([True] if waiting else [])
    if waiting:
        assert job.outcome == 'waiting'
        assert jobs.start_fresh_pool(ctx, parameters) is job
        assert len(discovered) == 1 and len(calls) == 1
        assert queued == [True] and refreshed == [{'force': True}]
    if failed:
        assert job.validation_code == 'fresh_preferences_changed'
        assert job.error == 'Your mix settings changed. Try finding fresh songs again.'
