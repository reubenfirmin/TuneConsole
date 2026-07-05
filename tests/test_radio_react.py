import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import radio


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema()
    return s


def test_skip_records_penalty_no_navigate_no_op(monkeypatch, store):
    monkeypatch.setattr(store, "identity_key_for_video", lambda vid: "kold")
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: {"kold": {"artist": "Aold"}})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    s = radio.RadioSession(); s.active = True
    # pos 5 / dur 200 -> ratio 0.025 <= 0.30 and pos <= 120 -> "skip".
    out = radio.react(store, s, {"kind": "track_exit", "videoId": "vold",
                                 "position": 5.0, "duration": 200.0}, now=100.0)
    assert out == {"desired_vids": None, "prime": None}   # react never mutates the playlist
    assert s.skips == [("Aold", None, 100.0)]             # only the model moved


def test_completion_is_inert(store):
    s = radio.RadioSession(); s.active = True
    out = radio.react(store, s, {"kind": "track_exit", "videoId": "v",
                                 "position": 190.0, "duration": 200.0}, now=100.0)   # ratio 0.95 -> completion
    assert out == {"desired_vids": None, "prime": None} and s.skips == []


def test_volume_floor_soft_skip(monkeypatch, store):
    monkeypatch.setattr(store, "identity_key_for_video", lambda vid: "kv")
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: {"kv": {"artist": "Av"}})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    s = radio.RadioSession(); s.active = True
    radio.react(store, s, {"kind": "volume", "videoId": "vv", "volume": 0.0}, now=100.0)
    assert s.skips == [("Av", None, 100.0)]


def test_volume_above_floor_is_noop(monkeypatch, store):
    s = radio.RadioSession(); s.active = True
    out = radio.react(store, s, {"kind": "volume", "videoId": "vv", "volume": 0.9}, now=100.0)
    assert out == {"desired_vids": None, "prime": None}
    assert s.skips == []


def test_rate_dislike_records_penalty(monkeypatch, store):
    monkeypatch.setattr(store, "identity_key_for_video", lambda vid: "kr")
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: {"kr": {"artist": "Ar"}})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    s = radio.RadioSession(); s.active = True
    out = radio.react(store, s, {"kind": "rate", "videoId": "vr",
                                 "url": "https://music.youtube.com/dislike"}, now=100.0)
    assert out == {"desired_vids": None, "prime": None}
    assert s.skips == [("Ar", None, 100.0)]


def test_rate_like_is_noop(store):
    s = radio.RadioSession(); s.active = True
    out = radio.react(store, s, {"kind": "rate", "videoId": "vr",
                                 "url": "https://music.youtube.com/like"}, now=100.0)
    assert out == {"desired_vids": None, "prime": None}
    assert s.skips == []


def test_bye_is_inert(store):
    s = radio.RadioSession(); s.active = True; s.pos = 2
    assert radio.react(store, s, {"kind": "bye"}, now=1.0) == {"desired_vids": None, "prime": None}
    assert s.active is True                               # WS disconnect is the real end, not bye


def test_inactive_session_is_noop(store):
    s = radio.RadioSession()   # active defaults to False
    out = radio.react(store, s, {"kind": "track_exit", "videoId": "v", "position": 5.0,
                                 "duration": 200.0}, now=100.0)
    assert out == {"desired_vids": None, "prime": None}
    assert s.skips == []


def test_react_never_raises_on_garbage(store):
    s = radio.RadioSession(); s.active = True
    assert radio.react(store, s, None, now=1.0) == {"desired_vids": None, "prime": None}


def test_react_never_raises_on_non_string_kind(store):
    s = radio.RadioSession(); s.active = True
    assert radio.react(store, s, {"kind": 123}, now=1.0) == {"desired_vids": None, "prime": None}
