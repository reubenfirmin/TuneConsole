"""Late acquisition and finalization must never publish into another draft generation."""
import threading

import pytest

from tests.web.test_road_trip_routes import (
    _app, _recipe, _recipe_form, _stub_pools, road_trip_schema, store,
)
from yt_playlist.rec import road_trip
from yt_playlist.web.routes import road_trip as routes
from tests.home_fixture_support import without_metadata_providers


@pytest.fixture(autouse=True)
def generation_routes(monkeypatch, without_metadata_providers):
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview

    # These requests own recipe generation/revision handling and the actual
    # Road Trip HTML. Other pages and provider configuration are lower inputs.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (app_update, audio_preview, routes)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)


@pytest.mark.parametrize('replacement', ['build', 'edit', 'balance', 'delete', 'delete-recipe'])
@pytest.mark.parametrize('old_failure', [False, True])
@pytest.mark.parametrize('phase', ['theirs', 'mine'])
def test_slow_worker_cannot_replace_or_finish_newer_work(store, monkeypatch, replacement, old_failure, phase):
    _stub_pools(monkeypatch, store)
    client, _ = _app(store)
    rid = _recipe(store)
    workers = []
    monkeypatch.setattr(routes, '_spawn', workers.append)
    if phase == 'mine':
        start = road_trip.start_draft

        def own_facts_build(*args, **kwargs):
            state = start(*args, **kwargs)
            state.update(pending=[], building=True, own_facts_left=1)
            return state

        monkeypatch.setattr(road_trip, 'start_draft', own_facts_build)
    assert client.post(f'/road_trip/recipes/{rid}/build').status_code == 200
    first = store.road_trip.get_road_trip_draft(rid)
    entered, release = threading.Event(), threading.Event()
    failures = []
    method = 'add_other_input' if phase == 'theirs' else 'fill_own_facts'
    acquire = getattr(road_trip, method)

    def paused(state, *args):
        if state['generation'] == first['generation']:
            entered.set()
            assert release.wait(5)
            if old_failure:
                raise RuntimeError('Old acquisition failed')
        return acquire(state, *args)

    monkeypatch.setattr(road_trip, method, paused)

    def run_old():
        try:
            workers[0]()
        except RuntimeError as exc:
            failures.append(str(exc))

    old = threading.Thread(target=run_old)
    old.start()
    try:
        assert entered.wait(1)
        if replacement == 'build':
            response = client.post(f'/road_trip/recipes/{rid}/build')
        elif replacement == 'edit':
            response = client.post('/road_trip/recipes', data={
                'id': str(rid), 'name': 'Edited trip', 'artists': '["Tame Impala"]',
                'genres': '[]', 'target_minutes': '60'})
        elif replacement == 'balance':
            response = client.post('/road_trip/recipes', data=_recipe_form(store, rid, own_pct=30, familiarity_pct=75))
        elif replacement == 'delete':
            response = client.delete(f'/road_trip/draft/{rid}')
        else:
            response = client.delete(f'/road_trip/recipes/{rid}')
        assert response.status_code == 200
        newer = store.road_trip.get_road_trip_draft(rid)
        if newer is not None:
            assert newer['generation'] != first['generation'] and newer['building']
            assert newer['pending'] or newer['own_facts_left']
            # Polling must recognize the replacement's registered job even
            # though it has not started, and must not settle it as orphaned.
            assert client.get(f'/road_trip/draft/{rid}/progress').status_code == 200
            assert store.road_trip.get_road_trip_draft(rid) == newer
    finally:
        release.set()
        old.join(5)
    assert not old.is_alive()
    assert failures == (['Old acquisition failed'] if old_failure else [])
    assert store.road_trip.get_road_trip_draft(rid) == newer
    # A late failure cannot annotate the replacement's rendered state either.
    if newer is not None:
        assert 'Old acquisition failed' not in client.get(f'/road_trip/draft/{rid}/progress').text
        assert store.road_trip.get_road_trip_draft(rid) == newer
    for worker in workers[1:]:
        worker()
    done = store.road_trip.get_road_trip_draft(rid)
    if newer is None:
        assert done is None
    else:
        assert done['generation'] == newer['generation']
        assert not done['building'] and not done['pending'] and not done['build_error']
        if replacement == 'edit':
            assert done['name'] == 'Edited trip' and done['target_minutes'] == 60
        if replacement == 'balance':
            recipe = store.road_trip.get_road_trip_recipe(rid)
            assert done['own_pct'] == recipe['own_pct'] == 30
            assert done['familiarity_pct'] == recipe['familiarity_pct'] == 75
    assert client.app.state.ctx.work.wait(0)


def test_overlapping_edits_invalidate_queued_workers_and_current_failure_settles(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    client, _ = _app(store)
    rid = _recipe(store)
    workers = []
    monkeypatch.setattr(routes, '_spawn', workers.append)
    client.post(f'/road_trip/recipes/{rid}/build')
    for pct in (20, 60, 80):
        client.post('/road_trip/recipes', data=_recipe_form(store, rid, own_pct=100-pct, familiarity_pct=pct))
    latest = store.road_trip.get_road_trip_draft(rid)
    calls = []

    def failed(state, *_args):
        calls.append(state['generation'])
        raise RuntimeError('Current acquisition failed')

    monkeypatch.setattr(road_trip, 'add_other_input', failed)
    for worker in workers[:-1]:
        worker()
    assert not calls and store.road_trip.get_road_trip_draft(rid) == latest
    with pytest.raises(RuntimeError, match='Current acquisition failed'):
        workers[-1]()
    done = store.road_trip.get_road_trip_draft(rid)
    assert calls == [latest['generation']] and done['own_pct'] == 20
    recipe = store.road_trip.get_road_trip_recipe(rid)
    assert recipe['own_pct'] == 20 and done['familiarity_pct'] == recipe['familiarity_pct'] == 80
    assert done['build_error'] == 'Current acquisition failed'
    assert not done['building'] and not done['pending']
    for url in (f'/road_trip/draft/{rid}/progress', '/road_trip'):
        response = client.get(url)
        assert response.status_code == 200
        assert 'Current acquisition failed' not in response.text
        assert "Couldn't finish adding their tracks" in response.text
        assert 'Save to YouTube' in response.text and 'Shuffle' in response.text
        assert store.road_trip.get_road_trip_draft(rid) == done
    assert client.app.state.ctx.work.wait(0)


def test_repository_rejects_old_revision_and_deleted_draft(store):
    store.accounts.upsert_account('main', 'cred', None, True)
    rid = _recipe(store)
    store.road_trip.save_road_trip_draft(rid, {'revision': 'new', 'value': 'new'}, 100)
    assert not store.road_trip.replace_road_trip_draft(
        rid, {'revision': 'old', 'value': 'old'}, 200, expected_revision='old')
    assert store.road_trip.get_road_trip_draft(rid)['value'] == 'new'
    store.road_trip.delete_road_trip_draft(rid)
    assert not store.road_trip.replace_road_trip_draft(
        rid, {'revision': 'new'}, 300, expected_revision='new')
    assert store.road_trip.get_road_trip_draft(rid) is None


@pytest.mark.parametrize('replacement', ['build', 'balance', 'delete', 'delete-recipe'])
def test_remote_save_completion_does_not_replace_newer_or_deleted_draft(store, monkeypatch, replacement):
    from types import SimpleNamespace
    from yt_playlist.core.activity import Activity
    from yt_playlist.web.road_trip_drafts import RoadTripDrafts

    store.accounts.upsert_account('main', 'cred', None, True)
    _stub_pools(monkeypatch, store)
    rid = _recipe(store)
    workers = []
    owner = RoadTripDrafts(SimpleNamespace(store=store, now_fn=lambda: 1000, work=Activity()), workers.append)
    owner.build(rid, object())
    _, saving = owner.for_save(rid)
    if replacement == 'build':
        owner.build(rid, object())
    elif replacement == 'balance':
        recipe = store.road_trip.get_road_trip_recipe(rid)
        owner.edit_recipe(lambda: store.road_trip.save_road_trip_recipe(
            rid, recipe['name'], 20, recipe['artists'], recipe['genres'],
            recipe['target_minutes'], 1000, familiarity_pct=80), object())
    else:
        owner.delete(rid, recipe=replacement == 'delete-recipe')
    newer = store.road_trip.get_road_trip_draft(rid)
    owner.saved(rid, saving, 'saved-old-picks')
    assert store.road_trip.get_road_trip_draft(rid) == newer
    recipe = store.road_trip.get_road_trip_recipe(rid)
    if replacement == 'delete-recipe':
        assert recipe is None
    else:
        assert recipe['last_playlist_id'] == 'saved-old-picks'
    owner.delete(rid)
    for worker in workers:
        worker()
