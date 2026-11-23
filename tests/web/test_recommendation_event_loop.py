"""Database contention in recommendation forms must leave the event loop usable."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from tests.conftest import FakeClient, _track, seed_catalog_track
from tests.home_fixture_support import without_metadata_providers
from tests.store_fixture_support import bounded_consumer_schema, without_query_timing
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Keep the Home controls, Jazz readiness reserve and Rock recipe inputs.
    with bounded_consumer_schema((('house', 'House'), ('jazz', 'Jazz'), ('rock', 'Rock'))) as schema:
        yield schema


@pytest.fixture(scope='module')
def earned_home_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.recommendation_support import publish_bundles, seed_library_favorites, seed_publication_subjects
    from tests.test_growth_journey import early_journey
    from yt_playlist.rec import taste_analysis

    def prepare(source):
        fresh = early_journey(source)
        pools = {}
        for lane in ('wheelhouse', 'explore', 'comfort'):
            pools[lane] = [
                {'key': f'{lane} {i}|artist {i}', 'title': f'{lane} {i}',
                 'artist': f'Artist {i}', 'video_id': f'{lane}-{i}', 'genre': 'House', 'score': .8}
                for i in range(12)]
        seed_publication_subjects(source, [track for pool in pools.values() for track in pool])
        seed_library_favorites(source, pools['comfort'])
        publish_bundles(source, {'all': {**pools, 'fresh': fresh},
                                 '_meta': {'unmodeled': True, 'full_pools': True}})
        taste_analysis.prepare_library_summary(source, 1000)

    with prepared_template(_pristine_current_schema, prepare) as template:
        yield template


@pytest.fixture
def form_routes(path, monkeypatch, without_metadata_providers):
    """Construct the real form owner without unrelated application routers."""
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_vibes, cleanup, clusters, home, road_trip, suggest, taste

    owners = {'home': home, 'clusters': clusters, 'overlaps': cleanup,
              'taste': taste, 'recs': suggest, 'road_trip': road_trip}
    owner = owners.get(path.split('/')[1])
    modules = (app_update, audio_vibes) + ((owner,) if owner else ())
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
    ('/home/generate', {'tracks': '[{"video_id":"song","title":"Song","artist":"Artist"}]'}),
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
        store.accounts.upsert_account('main', None, True)
        _stub_pools(monkeypatch, store)
        rid = _recipe(store, artists=[])
        draft = road_trip.start_draft(store, store.road_trip.get_road_trip_recipe(rid), 1000, seed=7)
        store.road_trip.save_road_trip_draft(rid, {**draft, 'revision': 'contention-fixture'}, 1000)
        data = _recipe_form(store, rid, own_pct=20, familiarity_pct=80)
    guarded_home = path.startswith('/home/') and path != '/home/generate'
    if path == '/home/vibe':
        from tests.test_growth_destinations import publish_full_journey

        publish_full_journey(store)
    elif guarded_home:
        request.getfixturevalue('earned_home_template').restore(store)
    clients = {}
    if path in ('/home/generate', '/now-playing/rate'):
        account = store.accounts.upsert_account('main', None, True)
        provider = FakeClient(catalog={'song': _track('song', 'Song', 'Artist')})
        clients[account] = provider
        store.tracks.upsert_track('song', 'Song', 'Artist', '', 180)
    if path in ('/recs/feedback', '/recs/mood'):
        seed_catalog_track(store, 'song', 'Song', 'Artist', video_id='song')
    if path == '/recs/journey':
        account = store.accounts.upsert_account('main', None, True)
        pid = store.playlists.upsert_playlist(account, 'PLJOURNEY', 'Journey', 0, '', 1000)
        assert pid == int(data['pid'])
        store.playlist_recipes.set_recipe(store.playlists.get_playlist(pid).playlist_scope_id,
                                          {'journey': 'shuffle'}, 1000)
    app = create_app(store, lambda: clients, now_fn=lambda: 1000, background_work=False)
    monkeypatch.setattr(app.state.ctx.bridge, 'send_control', lambda frame, *, intent: True)
    if path == '/home/vibe':
        from tests.web.test_vibe_event_loop import render_preset
        from yt_playlist.web.routes.home_views import HomeViews

        monkeypatch.setattr(HomeViews, 'cards_safe', render_preset)
    if guarded_home:
        from yt_playlist.web.routes.home_access import state
        growth = state(app.state.ctx)
        assert growth['ready_areas']['directions'], [
            (node['id'], node['usable'], node['ready']) for node in growth['nodes']]
        if path == '/home/vibe':
            assert growth['vibes_ready']

    @app.get('/_test/event-loop')
    async def probe():
        return {'ok': True}

    held, waiting, release = threading.Event(), threading.Event(), threading.Event()
    acquire = store._lock.acquire

    def observe_acquire(blocking=True, timeout=-1):
        if blocking and held.is_set() and not release.is_set():
            waiting.set()
        return acquire(blocking, timeout)

    monkeypatch.setattr(store._lock, 'acquire', observe_acquire)

    def hold_connection():
        with store._lock:
            held.set()
            release.wait(5)  # Bound a regression's stall so the test cannot deadlock.

    async def exercise():
        # ASGITransport does not run lifespan itself. Drain accepted work before
        # the fixture closes its database, including requests that start jobs.
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            # Startup warms its own database reads. Hold the lock only after
            # they finish so the request is tested against a live contention.
            holder = threading.Thread(target=hold_connection)
            holder.start()
            try:
                assert await asyncio.to_thread(held.wait, 1)
                started = time.monotonic()
                body = {'json': data} if path in ('/play', '/now-playing/rate') else {'data': data}
                form = asyncio.create_task(client.post(path, **body))
                try:
                    assert await asyncio.to_thread(waiting.wait, 1), 'Form did not attempt the held lock'
                    response = await client.get('/_test/event-loop')
                    assert response.status_code == 200
                    assert time.monotonic() - started < 1
                    assert not form.done(), 'Form completed before the operation was released'
                    assert f'request {path}' in app.state.ctx.work.active
                finally:
                    release.set()
                    result = await asyncio.wait_for(form, 5)
                return result
            finally:
                release.set()
                holder.join(1)
                assert not holder.is_alive()

    result = asyncio.run(exercise())
    expected = ((204,) if path == '/home/format' and data == {'panel': 'directions'} else
                (409,) if path in ('/home/format', '/home/growth/format') else
                (200, 400))
    assert result.status_code in expected
    if path in ('/play', '/now-playing/rate'):
        assert result.json()['ok'] is True
    if path == '/now-playing/rate':
        assert provider.rated == [('song', 'LIKE')]
        assert store.tracks.item_rating(account, 'song')['status'] == 'LIKE'
    if path == '/home/generate':
        assert result.status_code == 200
        assert len(provider.created) == 1
        playlist = store.playlists.get_playlist_for_account(account, provider.created[0][0])
        assert playlist is not None and playlist.track_count == 1
    if path == '/clusters/save':
        assert 'Keep at least one track to create a playlist.' in result.text
    if path == '/overlaps/suppress-many':
        assert len(store.overlaps.get_suppressed_overlap_pairs()) == 1
    if path == '/recs/feedback':
        assert store.rec_model.suppressed_keys('for_you', 1000, keys=['song']) == {'song'}
    if path == '/recs/mood':
        assert store.rec_signals.recent_mood_events() == [(1000, 1, ['song'])]
    if path == '/recs/journey':
        assert store.rec_model.get_session_weights(1000)['journey:shuffle'] == pytest.approx(1.15)
    if path == '/home/mix-genres':
        assert result.status_code == 200
        assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['house']
    if path == '/home/vibe':
        assert result.headers['X-Mix-Tuning-Active'] == 'false'
        assert store.settings.get_setting('home_mix_vibe') is None
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

    iid = store.accounts.upsert_account('main', None, True)
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
                assert 'save' in app.state.ctx.work.active
                assert 'request /clusters/save' in app.state.ctx.work.active
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
