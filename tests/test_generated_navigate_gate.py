"""A generated playlist's YouTube Music switch must wait until the playlist actually exists.

create_playlist returns a valid id before YouTube has propagated the playlist to be loadable at
watch?list=<id> (eventual consistency). Navigating (and, since #101, foregrounding) that tab
immediately lands on a not-yet-ready playlist. So poll get_playlist until it reports tracks, THEN
send the navigate. Best-effort: if it never confirms within the budget, skip the switch rather than
foreground a broken tab (the in-app redirect and the manual link still work).
"""
from yt_playlist.library import playlist_navigation
from yt_playlist.core.bridge import Bridge
import pytest


@pytest.fixture(autouse=True)
def recording_transport(monkeypatch):
    monkeypatch.setattr("yt_playlist.core.bridge.asyncio.run_coroutine_threadsafe", lambda frame, loop: None)


class _Bridge(Bridge):
    def __init__(self):
        super().__init__()
        self.sent = []
        self._send = self.sent.append


class _Client:
    """get_playlist returns empty until the Nth call, mimicking YouTube's propagation delay."""
    def __init__(self, ready_on_call):
        self.calls = 0
        self._ready_on = ready_on_call

    def get_playlist(self, playlist_id, limit=1):
        self.calls += 1
        if self.calls >= self._ready_on:
            return {"tracks": [{"videoId": "v1"}]}
        return {"tracks": []}


def test_navigates_once_the_playlist_reports_tracks():
    c, b, slept = _Client(ready_on_call=3), _Bridge(), []
    ok = playlist_navigation.navigate_when_ready(c, b, "PLX", "https://music.youtube.com/watch?list=PLX",
                                      intent=b.begin_playback_intent(), tries=20, sleep=slept.append)
    assert ok is True
    assert b.sent == [{"type": "navigate", "url": "https://music.youtube.com/watch?list=PLX"}]
    assert c.calls == 3                       # polled until ready, no further
    assert len(slept) == 2                    # slept between the two not-ready polls


def test_does_not_navigate_before_the_playlist_is_ready():
    c, b, slept = _Client(ready_on_call=99), _Bridge(), []
    ok = playlist_navigation.navigate_when_ready(c, b, "PLX", "url", intent=b.begin_playback_intent(),
                                                  tries=4, sleep=slept.append)
    assert ok is False
    assert b.sent == []                       # never confirmed within the budget -> no switch
    assert c.calls == 4                       # exhausted the try budget


def test_a_get_playlist_error_is_treated_as_not_ready_and_retried():
    class _Flaky:
        def __init__(self):
            self.calls = 0

        def get_playlist(self, playlist_id, limit=1):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("404 not found yet")
            return {"tracks": [{"videoId": "v1"}]}

    c, b = _Flaky(), _Bridge()
    assert playlist_navigation.navigate_when_ready(c, b, "PLX", "url", intent=b.begin_playback_intent(),
                                                   tries=5, sleep=lambda _s: None) is True
    assert c.calls == 2                        # the throw was retried, not fatal
    assert b.sent == [{"type": "navigate", "url": "url"}]


def test_newer_save_wins_when_playlists_become_ready_in_reverse_order():
    bridge = _Bridge()
    first_intent = bridge.begin_playback_intent()

    def newer_save(_seconds):
        intent = bridge.begin_playback_intent()
        assert playlist_navigation.navigate_when_ready(_Client(1), bridge, "B", "urlB", intent=intent)

    assert not playlist_navigation.navigate_when_ready(_Client(2), bridge, "A", "urlA",
        intent=first_intent, sleep=newer_save)
    assert bridge.sent == [{"type": "navigate", "url": "urlB"}]


def test_direct_play_during_read_invalidates_old_handoff_before_send():
    bridge = _Bridge()
    intent = bridge.begin_playback_intent()

    class ReadyAfterDirectPlay:
        def get_playlist(self, *args, **kwargs):
            bridge.send_control({"type": "navigate", "url": "direct"})
            return {"tracks": [{"videoId": "v"}]}

    assert not playlist_navigation.navigate_when_ready(ReadyAfterDirectPlay(), bridge, "A", "old", intent=intent)
    assert bridge.sent == [{"type": "navigate", "url": "direct"}]


def test_home_captures_intent_before_creation_finishes(store, monkeypatch):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient
    from yt_playlist.web.app import create_app

    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = FakeClient()
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1)
    bridge = app.state.ctx.bridge
    sent = []
    bridge._send = sent.append
    entered, release = threading.Event(), threading.Event()

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"new_ytm": "OLD", "pid": None, "added": 1}

    monkeypatch.setattr("yt_playlist.library.playlist_generated.create_generated_playlist", create)
    with TestClient(app, base_url="http://127.0.0.1") as web:
        with ThreadPoolExecutor(1) as pool:
            saving = pool.submit(web.post, "/home/generate", data={"name": "Old", "tracks": json.dumps([{"video_id": "v"}])})
            try:
                assert entered.wait(5)
                assert web.post("/play", json={"url": "https://music.youtube.com/watch?v=new"}).json()["ok"]
            finally:
                release.set()
            assert saving.result(5).status_code == 200
    assert sent == [{"type": "navigate", "url": "https://music.youtube.com/watch?v=new"}]
