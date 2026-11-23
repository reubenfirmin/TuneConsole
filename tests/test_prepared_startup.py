"""Home serves during preparation of an empty library."""

from fastapi.testclient import TestClient
import pytest

from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import profile_files
from yt_playlist.rec import home_prepared, model_refresh, vibe_pools, vibe_readiness
from yt_playlist.rec.worker_queue import Job
from yt_playlist.web import app as web_app


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Startup consumes an empty library, independent of genre breadth.
    with bounded_consumer_schema((('rock', 'Rock'),)) as schema:
        yield schema


def forbid_preparation(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('Startup requests must only read prepared output')
    for owner, name in ((home_prepared, 'prepare'), (model_refresh, 'rebuild'),
                        (vibe_pools, 'prepare'), (vibe_pools, 'assemble'),
                        (vibe_readiness, 'prepare'), (profile_files, 'read')):
        monkeypatch.setattr(owner, name, unexpected)
    return unexpected


def test_empty_preparation_serves_home_with_missing_features_and_preparation_notice(store, monkeypatch):
    forbidden = forbid_preparation(monkeypatch)
    app = web_app.create_app(store, forbidden, background_work=False)
    app.state.ctx.rec_worker.jobs.merge(Job.PREPARE)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/')
        assert response.status_code == 200
        assert 'Preparing library data. Features will appear as they are ready.' in response.text
        growth = client.get('/home/growth').json()
        assert not any(growth['ready_formats'].values()) and not growth['vibes_ready']
