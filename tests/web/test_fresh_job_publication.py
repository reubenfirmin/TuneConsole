"""Explicit discovery jobs must not report an uncommitted ranking as complete."""
import logging
from types import SimpleNamespace

import pytest

from yt_playlist.rec import fresh_pool
from yt_playlist.web import jobs


@pytest.mark.parametrize('results,expected_calls,failed', [
    ([None, {}], 2, False), ([{}, None], 1, False), ([None] * 3, 3, True),
])
def test_fresh_job_retries_deferred_publication_without_repeating_discovery(
        monkeypatch, results, expected_calls, failed):
    job = SimpleNamespace(events=[], done=False, error=None)
    calls, discovered, refreshed = [], [], []
    pending = iter(results)
    monkeypatch.setattr(fresh_pool, 'populate', lambda *args, **kwargs: discovered.append(kwargs))
    def prepare(*args):
        calls.append(args)
        return next(pending)
    monkeypatch.setattr(fresh_pool, 'prepare', prepare)
    monkeypatch.setattr(jobs, 'activity_for', lambda ctx: SimpleNamespace(start_thread=lambda run: run()))
    ctx = SimpleNamespace(store=object(), now_fn=lambda: 1,
        jobs=SimpleNamespace(create_or_rejoin=lambda *args: (job, True)),
        rec_worker=SimpleNamespace(trigger_home_refresh=lambda **kwargs: refreshed.append(kwargs)),
        logger=logging.getLogger(__name__))
    assert jobs.start_fresh_pool(ctx, {'include_genres': ['jazz'], 'avoid_genres': []}) is job
    assert job.done and len(discovered) == 1 and len(calls) == expected_calls
    assert bool(job.error) == failed
    assert refreshed == ([] if failed else [{'force': True}])
    if failed:
        assert job.validation_code == 'fresh_preferences_changed'
        assert job.error == 'Your mix settings changed. Try finding fresh songs again.'
