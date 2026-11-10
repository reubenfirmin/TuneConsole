"""Database contention in recommendation forms must leave the event loop usable."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from tests.home_fixture_support import without_metadata_providers
from tests.store_fixture_support import bounded_consumer_schema, without_query_timing
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # These requests own thread handoff and actual SQL contention. Preserve the
    # House controls and Rock recipe inputs; catalog curation has separate owners.
    with bounded_consumer_schema((('house', 'House'), ('rock', 'Rock'))) as schema:
        yield schema


@pytest.fixture
def form_routes(path, monkeypatch, without_metadata_providers):
    """Construct the real form owner without unrelated application routers."""
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, cleanup, clusters, home, road_trip, suggest, taste

    owners = {'home': home, 'clusters': clusters, 'overlaps': cleanup,
              'taste': taste, 'recs': suggest, 'road_trip': road_trip}
    owner = owners.get(path.split('/')[1])
    modules = (app_update, audio_preview) + ((owner,) if owner else ())
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in modules])
    if path not in ('/play', '/now-playing/rate'):
        monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())


@pytest.mark.parametrize('path,data', [
    ('/home/fresh-pool', {'include_genres': 'unknown'}),
    ('/home/library-pool', {'lane': 'wheelhouse', 'epoch': '-1'}),
    ('/home/other-genres', {'genres': '["house"]'}),
    ('/home/mix-genres', {'genres': 'house'}),
    ('/home/vibe', {'vibe': ''}),
    ('/home/breadth', {'breadth_bias': '0.2'}),
    ('/home/steer', {'axis': 'genre:house', 'weight': '1.2'}),
    ('/home/fingerprint/add', {'axis': 'genre:house'}),
    ('/home/fingerprint/remove', {'axis': 'genre:house'}),
    ('/home/format', {'panel': 'directions'}),
    ('/home/format', {'panel': 'radio'}),
    ('/home/format', {'format': 'radio'}),
    ('/home/growth/format', {'format': 'radio'}),
    ('/home/generate', {'tracks': '[]'}),
    ('/clusters/save', {'keep_keys': '[]'}),
    ('/overlaps/suppress-many', {'pairs': '[["PLA","PLB"]]'}),
    ('/taste/weight', {'axis': 'genre:house', 'weight': '0.5'}),
    ('/taste/param', {'name': 'modes_k', 'value': '3'}),
    ('/taste/reset-param', {'name': 'modes_k'}),
    ('/taste/unban', {'key': 'song'}),
    ('/recs/feedback', {'item': 'song', 'kind': 'dismiss'}),
    ('/recs/mood', {'keys': '["song"]', 'dir': '1'}),
    ('/recs/journey', {'pid': '1', 'dir': '1'}),
    ('/play', {'url': 'https://music.youtube.com/watch?v=song'}),
    ('/now-playing/rate', {'action': 'like', 'videoId': 'song'}),
    ('/road_trip/recipes', {'name': 'Trip', 'artists': '[]', 'genres': '[]'}),
    ('/road_trip/recipes', {'id': 'existing', 'own_pct': '20', 'familiarity_pct': '80'}),
    ('/road_trip/draft/1/tilt', {'party': 'theirs', 'axis': 'genre:house', 'share': '50'}),
    ('/road_trip/draft/1/tilt/v2', {'party': 'theirs', 'axis': 'genre:house', 'share': '50'}),
    ('/road_trip/draft/1/unpin', {'party': 'theirs', 'axis': 'genre:house'}),
    ('/road_trip/draft/1/save', {}),
])
def test_forms_wait_for_database_outside_event_loop(store, monkeypatch, request, path, data, form_routes):
    existing_draft = data.get('id') == 'existing'
    if existing_draft:
        from tests.web.test_road_trip_routes import _recipe, _recipe_form, _stub_pools
        from yt_playlist.rec import road_trip
        store.accounts.upsert_account('main', 'cred', None, True)
        _stub_pools(monkeypatch, store)
        rid = _recipe(store, artists=[])
        draft = road_trip.start_draft(store, store.road_trip.get_road_trip_recipe(rid), 1000, seed=7)
        store.road_trip.save_road_trip_draft(rid, {**draft, 'revision': 'contention-fixture'}, 1000)
        data = _recipe_form(store, rid, own_pct=20, familiarity_pct=80)
    guarded_home = path.startswith('/home/') and path != '/home/generate'
    if guarded_home:
        # Completed readiness is below the form's thread handoff. Retained
        # access checks still open the real database and must wait for the
        # actual connection lock held below. Keep accepted settings writes real.
        request.getfixturevalue('unlocked_home_controls')
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    monkeypatch.setattr(app.state.ctx.bridge, 'send_control', lambda frame, *, intent: True)
    if path == '/home/vibe':
        # The request's real admission/settings writes must wait off the event
        # loop. Numerical menu generation is a lower-layer collaborator, owned
        # by test_vibe_requests; it need not run for this contention check.
        monkeypatch.setattr(app.state.ctx.vibe_requests, '_build', lambda request: None)
    if guarded_home:
        from yt_playlist.web.routes.home_access import state
        growth = state(app.state.ctx)
        assert growth['ready_areas']['directions']
        if path == '/home/vibe':
            assert growth['vibes_ready']

    @app.get('/_test/event-loop')
    async def probe():
        return {'ok': True}

    held, release = threading.Event(), threading.Event()

    def hold_connection():
        with store._lock:
            held.set()
            release.wait(5)  # Bound a regression's stall so the test cannot deadlock.

    holder = threading.Thread(target=hold_connection)
    holder.start()
    assert held.wait(1)

    async def exercise():
        # ASGITransport does not run lifespan itself. Drain accepted work before
        # the fixture closes its database, including requests that start jobs.
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            started = time.monotonic()
            body = {'json': data} if path in ('/play', '/now-playing/rate') else {'data': data}
            request = asyncio.create_task(client.post(path, **body))
            try:
                await asyncio.sleep(0.05)
                response = await client.get('/_test/event-loop')
                assert response.status_code == 200
                assert time.monotonic() - started < 1
                assert not request.done(), 'Form did not exercise a database wait'
            finally:
                release.set()
                result = await asyncio.wait_for(request, 5)
            expected = ((204,) if path == '/home/format' and data == {'panel': 'directions'} else
                        (409,) if path in ('/home/format', '/home/growth/format') else
                        (200, 400))
            assert result.status_code in expected
            if path == '/play':
                assert result.json()['ok'] is True
            if path == '/home/vibe':
                accepted = app.state.ctx.vibe_requests.state()
                assert accepted['id'] == result.headers['X-Home-Vibe-Request']
                assert accepted['vibe'] == '' and accepted['phase'] == 'pending'

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        holder.join(1)
    assert not holder.is_alive()
    if existing_draft:
        for state in (store.road_trip.get_road_trip_recipe(rid),
                      store.road_trip.get_road_trip_draft(rid)):
            assert state['own_pct'] == 20 and state['familiarity_pct'] == 80
        assert store.road_trip.get_road_trip_draft(rid)['seed'] == 7


def test_road_trip_metadata_lookup_does_not_block_async_requests(store, monkeypatch):
    from yt_playlist.rec import road_trip_candidates

    app = create_app(store, lambda: {}, background_work=False)
    entered, release = threading.Event(), threading.Event()

    def delayed_genre(_store, _artist):
        entered.set()
        assert release.wait(5), 'Metadata lookup was not released'
        return 'House'

    monkeypatch.setattr(road_trip_candidates, 'artist_genre', delayed_genre)

    @app.get('/_test/event-loop')
    async def probe():
        return {'ok': True}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            request = asyncio.create_task(client.post('/road_trip/recipes', data={
                'name': 'Trip', 'artists': '["Uncached Artist"]', 'genres': '[]'}))
            try:
                # A bounded thread wait also lets a regression unblock instead
                # of hanging the event loop before the probe can run.
                started = time.monotonic()
                assert await asyncio.to_thread(entered.wait, 1)
                response = await client.get('/_test/event-loop')
                assert response.status_code == 200
                assert time.monotonic() - started < 1
                assert not request.done()
                assert 'request /road_trip/recipes' in app.state.ctx.work.active
            finally:
                release.set()
                saved = await asyncio.wait_for(request, 5)
            assert saved.status_code == 200

    asyncio.run(exercise())
    recipe = store.road_trip.list_road_trip_recipes()[0]
    assert recipe['artists'] == ['Uncached Artist'] and recipe['genres'] == ['House']


@pytest.mark.parametrize('phase', ['ordering', 'bookkeeping'])
def test_cluster_save_keeps_event_loop_responsive_throughout(store, monkeypatch, phase):
    from tests.conftest import FakeClient, _track
    from yt_playlist.rec import measurement, recipes
    from yt_playlist.util.matching import identity_key

    iid = store.accounts.upsert_account('main', 'cred', None, True)
    client = FakeClient(catalog={'v1': _track('v1', 'Song', 'Artist', set_video_id='set-v1')})
    store.tracks.upsert_track('v1', 'Song', 'Artist', '', 180)
    key = identity_key('Song', 'Artist')
    app = create_app(store, lambda: {iid: client}, background_work=False)
    entered, release = threading.Event(), threading.Event()
    target, name = ((recipes, 'cluster_recipe') if phase == 'ordering' else (measurement, 'record_action'))
    original = getattr(target, name)

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Cluster save was not released'
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, delayed)

    @app.get('/_test/event-loop')
    async def probe():
        return {'ok': True}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as http:
            started = time.monotonic()
            request = asyncio.create_task(http.post('/clusters/save', data={
                'name': 'Mix', 'keep_keys': json.dumps([key]), 'journey': 'shuffle'}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                response = await http.get('/_test/event-loop')
                assert response.status_code == 200 and time.monotonic() - started < 1
                assert not request.done()
                assert '_save_cluster' in app.state.ctx.work.active
            finally:
                release.set()
                result = await asyncio.wait_for(request, 5)
            assert result.status_code == 200
            assert len(client.created) == 1
            ytm = client.created[0][0]
            playlist = store.playlists.get_playlist_for_account(iid, ytm)
            assert playlist is not None and playlist.track_count == 1
            assert f'data-generated-playlist="{ytm}"' in result.text
            assert f'window.location.assign("/playlist/{playlist.id}")' in result.text
            assert result.headers['HX-Trigger'] == 'cluster-saved'
            assert client.added == [(ytm, ['v1'])]
            assert [(track[0], track[1]) for track in store.playlists.get_playlist_tracks_with_meta(
                playlist.id)] == [(key, 'v1')]

    asyncio.run(exercise())
    assert len(client.created) == 1
