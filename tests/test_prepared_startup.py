"""HTTP admission uses prepared output; catch-up belongs behind the storage gate."""
import threading

from fastapi.testclient import TestClient
import pytest

from tests.home_fixture_support import home_routes_only, without_metadata_providers
from tests.store_fixture_support import bounded_consumer_schema

from yt_playlist.core import profile_files
from yt_playlist.rec import home_prepared, model_refresh, vibe_pools, vibe_readiness
from yt_playlist.web import app as web_app


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Startup gating consumes an empty library, independent of genre breadth.
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
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/')
        assert response.status_code == 200
        assert 'Preparing library data. Features will appear as they are ready.' in response.text
        growth = client.get('/home/growth').json()
        assert not any(growth['ready_formats'].values()) and not growth['vibes_ready']


def test_startup_serves_http_before_workers_are_allowed_to_catch_up(
        store, monkeypatch, home_routes_only, without_metadata_providers):
    forbidden = forbid_preparation(monkeypatch)
    app = web_app.create_app(store, forbidden, background_work=True)
    ctx = app.state.ctx
    store.activity_paused = True
    ctx.storage_maintenance.ready.clear()
    ctx.storage_maintenance.active = True
    monkeypatch.setattr(ctx.storage_maintenance, 'start', lambda: None)
    calls, caught_up = [], threading.Event()
    monkeypatch.setattr(ctx.radio, 'prepare', lambda: calls.append('radio'))
    monkeypatch.setattr(ctx.rec_worker, 'start_ticker', lambda: calls.append('recommendations'))
    monkeypatch.setattr(ctx.enrich_worker, 'start_ticker', lambda: calls.append('enrichment'))
    monkeypatch.setattr(ctx.rec_worker, 'trigger_mode_refresh', caught_up.set)
    monkeypatch.setattr(web_app, '_background_sync_loop', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, '_background_update_loop', lambda *args, **kwargs: None)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.get('/').status_code == 200
        assert client.get('/home/growth').status_code == 200
        assert calls == [] and not caught_up.is_set()
        store.activity_paused = False
        ctx.storage_maintenance.active = False
        ctx.storage_maintenance.ready.set()
        assert caught_up.wait(3)
        assert calls == ['radio', 'recommendations', 'enrichment']
