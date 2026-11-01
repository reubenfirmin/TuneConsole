"""An older worker must not turn an account-bound skip into a video-only click."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.core.bridge import Bridge
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import _now_playing_snapshot


@pytest.mark.parametrize('capability', [None, False, True])
@pytest.mark.parametrize('paused', [False, True])
def test_skull_requires_this_workers_account_bound_next_capability(tmp_path, monkeypatch,
                                                                 capability, paused):
    store = Store(tmp_path / 'library.db')
    try:
        store.init_schema()
        account = store.accounts.upsert_account('Main', 'bridge', 'main-brand', True)
        store.tracks.upsert_track('song', 'Song', 'Artist', None, 180)
        remote = FakeClient()
        app = create_app(store, lambda: {account: remote}, background_work=False)
        ctx, commands, radio_calls = app.state.ctx, [], []
        ctx.bridge._send = lambda _: None
        ctx.bridge.account_bound_next = capability
        ctx.bridge.now_playing = _now_playing_snapshot({'videoId': 'song', 'position': 20,
            'duration': 180, 'brandKnown': True, 'brandId': 'main-brand'})
        store._evidence_recovery_pending = paused

        async def control(frame, timeout=10):
            commands.append(frame)
            return 200, '{"ok":true}'

        def radio_skip(video, **options):
            radio_calls.append((video, options))
            return None

        monkeypatch.setattr(ctx.bridge, 'request_control', control)
        monkeypatch.setattr(ctx.radio, 'skip', radio_skip)
        result = TestClient(app, base_url='http://127.0.0.1').post('/now-playing/rate', json={
            'videoId': 'song', 'action': 'dislike', 'likeStatus': 'DISLIKE'}).json()
        assert result['ok'] and remote.rated == [('song', 'DISLIKE')]
        if capability is True:
            assert 'player_error' not in result
            assert [command['type'] for command in commands] == ['rate', 'next']
            assert commands[-1]['brandId'] == 'main-brand'
            assert radio_calls == ([] if paused else [('song', {'brand_id': 'main-brand'})])
        else:
            assert 'extension' in result['player_error'].lower()
            assert [command['type'] for command in commands] == ['rate']
            assert radio_calls == [], 'An unbound Radio fallback would bypass the account fence'
    finally:
        store.conn.close()


def test_reconnect_does_not_inherit_a_previous_workers_account_bound_capability():
    bridge = Bridge()

    async def send(frame):
        pass

    assert bridge.account_bound_next is None
    old = bridge.connect(send, None)
    bridge.account_bound_next = True
    current = bridge.connect(send, None)
    assert bridge.account_bound_next is None
    bridge.account_bound_next = True
    assert not bridge.disconnect(old)
    assert bridge.account_bound_next is True
    assert bridge.disconnect(current)
    assert bridge.account_bound_next is None
