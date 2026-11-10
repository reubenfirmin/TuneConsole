"""An older worker must not turn an account-bound skip into a video-only click."""
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.playback_feedback_support import held_copy
from yt_playlist.core import playback_feedback
from yt_playlist.core.bridge import Bridge
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import _now_playing_snapshot


@pytest.mark.parametrize('capability', [None, False, True])
@pytest.mark.parametrize('maintenance', ['available', 'cleanup', 'copy'])
def test_skull_requires_this_workers_account_bound_next_capability(file_backed_store, monkeypatch,
                                                                 capability, maintenance):
    store = file_backed_store
    account = store.accounts.upsert_account('Main', 'bridge', 'main-brand', True)
    store.tracks.upsert_track('song', 'Song', 'Artist', None, 180)
    remote = FakeClient()
    app = create_app(store, lambda: {account: remote}, background_work=False)
    ctx, commands, radio_calls = app.state.ctx, [], []
    ctx.bridge._send = lambda _: None
    ctx.bridge.account_bound_next = capability
    ctx.bridge.now_playing = _now_playing_snapshot({'videoId': 'song', 'position': 20,
        'duration': 180, 'brandKnown': True, 'brandId': 'main-brand'})
    store._evidence_recovery_pending = maintenance == 'cleanup'

    async def control(frame, timeout=10):
        commands.append(frame)
        return 200, '{"ok":true}'

    def radio_skip(video, *, brand_id):
        radio_calls.append((video, {'brand_id': brand_id}))
        return None

    monkeypatch.setattr(ctx.bridge, 'request_control', control)
    monkeypatch.setattr(ctx.radio, 'skip', radio_skip)
    paused = maintenance == 'copy'
    with held_copy(store) if paused else nullcontext():
        status = ctx.storage_maintenance.status()
        assert store.activity_paused is paused and status['active'] is paused
        assert status['recording'] is not paused
        assert status['evidence_maintenance']['pending'] is (maintenance == 'cleanup')
        result = TestClient(app, base_url='http://127.0.0.1').post('/now-playing/rate', json={
            'videoId': 'song', 'action': 'dislike', 'likeStatus': 'DISLIKE'}).json()
        assert result['ok'] and remote.rated == [('song', 'DISLIKE')]
        assert commands[0] == {'type': 'rate', 'action': 'dislike', 'videoId': 'song',
                               'likeStatus': 'DISLIKE', 'brandId': 'main-brand', 'skip': False}
        if capability is True:
            assert 'player_error' not in result
            assert [command['type'] for command in commands] == ['rate', 'next']
            assert commands[-1] == {'type': 'next', 'videoId': 'song', 'brandId': 'main-brand'}
            assert radio_calls == ([] if paused else [('song', {'brand_id': 'main-brand'})])
        else:
            assert result['player_error'] == 'Rating saved. Some updates are not confirmed; check it in YouTube Music.'
            assert [command['type'] for command in commands] == ['rate']
            assert radio_calls == [], 'An unbound Radio fallback would bypass the account fence'
        expected = 2 if capability is True else 1
        assert playback_feedback.inbox(store).pending() == (expected if paused else 0)
        assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == (
            0 if paused else expected)
    if paused:
        assert playback_feedback.drain(store) == expected
        assert playback_feedback.drain(store) == 0
    assert store.tracks.item_rating(account, 'song')['status'] == 'DISLIKE'


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
