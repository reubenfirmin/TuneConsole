"""Database contention in recommendation forms must leave the event loop usable."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from yt_playlist.web.app import create_app


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
    ('/road_trip/draft/1/tilt', {'party': 'theirs', 'axis': 'genre:house', 'share': '50'}),
    ('/road_trip/draft/1/tilt/v2', {'party': 'theirs', 'axis': 'genre:house', 'share': '50'}),
    ('/road_trip/draft/1/unpin', {'party': 'theirs', 'axis': 'genre:house'}),
    ('/road_trip/draft/1/mix', {'their_pct': '50'}),
    ('/road_trip/draft/1/save', {}),
])
def test_forms_wait_for_database_outside_event_loop(store, monkeypatch, path, data):
    app = create_app(store, lambda: {}, background_work=False)
    monkeypatch.setattr(app.state.ctx.bridge, 'send_control', lambda frame, *, intent: True)

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
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
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
            assert result.status_code in (200, 400)
            if path == '/play':
                assert result.json()['ok'] is True

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        holder.join(1)
    assert not holder.is_alive()


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

    iid = store.identities.upsert_identity('main', 'cred', None, True)
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
            assert result.status_code == 200 and 'Saved' in result.text
            assert result.headers['HX-Trigger'] == 'cluster-saved'

    asyncio.run(exercise())
    assert len(client.created) == 1
