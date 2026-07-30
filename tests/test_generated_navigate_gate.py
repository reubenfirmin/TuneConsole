"""A generated playlist's YouTube Music switch must wait until the playlist actually exists.

create_playlist returns a valid id before YouTube has propagated the playlist to be loadable at
watch?list=<id> (eventual consistency). Navigating (and, since #101, foregrounding) that tab
immediately lands on a not-yet-ready playlist. So poll get_playlist until it reports tracks, THEN
send the navigate. Best-effort: if it never confirms within the budget, skip the switch rather than
foreground a broken tab (the in-app redirect and the manual link still work).
"""
from yt_playlist.library import executor


class _Bridge:
    def __init__(self):
        self.sent = []

    def send_control(self, payload):
        self.sent.append(payload)


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
    ok = executor.navigate_when_ready(c, b, "PLX", "https://music.youtube.com/watch?list=PLX",
                                      tries=20, sleep=slept.append)
    assert ok is True
    assert b.sent == [{"type": "navigate", "url": "https://music.youtube.com/watch?list=PLX"}]
    assert c.calls == 3                       # polled until ready, no further
    assert len(slept) == 2                    # slept between the two not-ready polls


def test_does_not_navigate_before_the_playlist_is_ready():
    c, b, slept = _Client(ready_on_call=99), _Bridge(), []
    ok = executor.navigate_when_ready(c, b, "PLX", "url", tries=4, sleep=slept.append)
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
    assert executor.navigate_when_ready(c, b, "PLX", "url", tries=5, sleep=lambda _s: None) is True
    assert c.calls == 2                        # the throw was retried, not fatal
    assert b.sent == [{"type": "navigate", "url": "url"}]
