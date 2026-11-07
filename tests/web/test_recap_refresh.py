"""Legacy Wrapped payloads request monthly preparation without article work."""
from copy import deepcopy

import pytest

from tests.test_mode_refresh_worker import _worker
from tests.test_trends_recap_route import _client, _DETAIL_BEATS, _STORY
from yt_playlist.rec.recap_details import slides
from yt_playlist.rec.rec_worker import Job


@pytest.mark.parametrize('grouped', [True, False])
def test_recap_refresh_is_owned_by_monthly_projection_and_coalesces(_client, monkeypatch, grouped):
    beats = [*_STORY['beats'][:-1], *deepcopy(_DETAIL_BEATS), _STORY['beats'][-1]]
    story = {**_STORY, 'beats': beats if grouped else slides(beats)}
    client, store = _client(story)
    worker = _worker(store)
    original_worker = client.app.state.ctx.rec_worker
    launches = []
    monkeypatch.setattr(worker, '_start_loop', lambda: launches.append(True))
    client.app.state.ctx.rec_worker = worker
    try:
        for _ in range(3):
            response = client.get('/trends/story/2026-06')
            assert response.status_code == 200
            assert response.text.count('class="slide slide-new_this_month"') == 3
            assert store.rec_proposals.get_proposals('trend_rollups')['story'] == story
        assert worker.jobs.has(Job.TRENDS) == grouped
        assert worker.projections.active == ({'trends'} if grouped else set())
        assert launches == ([True] if grouped else [])
        assert not worker.jobs.has(Job.STORIES)
    finally:
        worker.shutdown()
        worker.join(1)
        client.app.state.ctx.rec_worker = original_worker


@pytest.mark.parametrize('availability', ['absent', 'disabled', 'stopping'])
def test_cached_recap_still_renders_when_worker_cannot_accept(_client, availability, monkeypatch):
    story = {**_STORY, 'beats': [*_STORY['beats'][:-1], *deepcopy(_DETAIL_BEATS), _STORY['beats'][-1]]}
    client, store = _client(story)
    worker = _worker(store)
    original_worker = client.app.state.ctx.rec_worker
    if availability == 'disabled':
        worker.enabled = False
    if availability == 'stopping':
        worker.shutdown()
    monkeypatch.setattr(worker, '_start_loop', lambda: pytest.fail('Unavailable worker started work'))
    client.app.state.ctx.rec_worker = None if availability == 'absent' else worker
    try:
        response = client.get('/trends/story/2026-06')
        assert response.status_code == 200
        assert response.text.count('class="slide slide-new_this_month"') == 3
        assert store.rec_proposals.get_proposals('trend_rollups')['story'] == story
        assert not worker.jobs.pending
    finally:
        worker.shutdown()
        worker.join(1)
        client.app.state.ctx.rec_worker = original_worker
