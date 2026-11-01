"""Mix playback must reach the paused-safe generator through the HTTP guard."""
import asyncio
import json
import threading

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('phase', ['compacting', 'checkpoint'])
def test_mix_launch_during_storage_maintenance_preserves_readonly_local_state(store, phase):
    account = store.accounts.upsert_account('Main', 'bridge', None, True)
    provider = FakeClient(catalog={'song': _track('song', 'Song', 'Artist', 180)})
    app = create_app(store, lambda: {account: provider}, background_work=False)
    ctx = app.state.ctx
    frames, navigated = [], threading.Event()

    async def send(frame):
        frames.append(frame)
        navigated.set()

    # Exercise the actual middleware state used during maintenance, rather
    # than only the generator's separate activity_paused branch.
    ctx.storage_maintenance.active = True
    ctx.storage_maintenance.phase = phase
    store.activity_paused = True
    before = store.conn.total_changes
    store.conn.execute('PRAGMA query_only=ON')
    try:
        with TestClient(app, base_url='http://127.0.0.1') as client:
            async def connect():
                ctx.bridge.connect(send, asyncio.get_running_loop())
            client.portal.call(connect)
            assert client.get('/storage-maintenance/status').json()['active']
            assert client.post('/sync').status_code == 503
            response = client.post('/home/generate', data={
                'name': 'Maintenance mix', 'launch_id': 'maintenance-check',
                'recipe': json.dumps({'lane': 'fresh', 'mode_id': 1}),
                'tracks': json.dumps([{'video_id': 'song', 'title': 'Song', 'artist': 'Artist'}])})
            assert response.status_code == 200, response.text
            assert 'Saved' in response.text and 'genComplete(' in response.text
            assert navigated.wait(5)
        assert len(provider.created) == len(frames) == 1
        playlist = provider.created[0][0]
        assert frames == [{'type': 'navigate', 'url': 'https://music.youtube.com/watch?list=' + playlist}]
        assert [t['videoId'] for t in provider._tracks[playlist]] == ['song']
        assert store.conn.total_changes == before
        assert store.playlists.get_playlists() == []
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False
        ctx.storage_maintenance.active = False
