"""Legacy Wrapped payloads request monthly preparation without article work."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.test_mode_refresh_worker import _worker
from tests.home_fixture_support import home_page_bytecode
from tests.test_trends_recap_route import (
    _client, _DETAIL_BEATS, _STORY, _pristine_current_schema, recap_routes,
)
from yt_playlist.rec.recap_details import slides
from yt_playlist.rec.rec_worker import Job
from yt_playlist.library.analytics_composition import stories
from yt_playlist.web.routes import trends


def bind_worker(monkeypatch, configure):
    state = {}
    def bind(ctx):
        worker = state['worker'] = _worker(ctx.store)
        available = configure(worker)
        return stories(SimpleNamespace(store=ctx.store, rec_worker=worker if available else None))
    monkeypatch.setattr(trends, 'stories', bind)
    return state


@pytest.mark.parametrize('grouped', [True, False])
def test_recap_refresh_is_owned_by_monthly_projection_and_coalesces(_client, monkeypatch, grouped):
    beats = [*_STORY['beats'][:-1], *deepcopy(_DETAIL_BEATS), _STORY['beats'][-1]]
    story = {**_STORY, 'beats': beats if grouped else slides(beats)}
    launches = []
    def configure(worker):
        monkeypatch.setattr(worker, '_start_loop', lambda: launches.append(True))
        return True
    state = bind_worker(monkeypatch, configure)
    client, store = _client(story, snapshot_guards=True)
    worker = state['worker']
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


@pytest.mark.parametrize('availability', ['absent', 'disabled', 'stopping'])
def test_cached_recap_still_renders_when_worker_cannot_accept(_client, availability, monkeypatch):
    story = {**_STORY, 'beats': [*_STORY['beats'][:-1], *deepcopy(_DETAIL_BEATS), _STORY['beats'][-1]]}
    def configure(worker):
        if availability == 'disabled':
            worker.enabled = False
        if availability == 'stopping':
            worker.shutdown()
        monkeypatch.setattr(worker, '_start_loop', lambda: pytest.fail('Unavailable worker started work'))
        return availability != 'absent'
    state = bind_worker(monkeypatch, configure)
    client, store = _client(story)
    worker = state['worker']
    try:
        response = client.get('/trends/story/2026-06')
        assert response.status_code == 200
        assert response.text.count('class="slide slide-new_this_month"') == 3
        assert store.rec_proposals.get_proposals('trend_rollups')['story'] == story
        assert not worker.jobs.pending
    finally:
        worker.shutdown()
        worker.join(1)
