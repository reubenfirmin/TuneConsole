import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import radio, rec_params


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema()
    return s


def _picker(seq):
    it = iter(seq)
    return lambda st, se, now: next(it, None)


def _pk(n):
    return {"key": "k" + n, "video_id": "v" + n, "artist": "a" + n, "title": "t" + n, "url": "u" + n}


def _started(monkeypatch, store, size=3, n=6):
    rec_params.set_param(store, "radio_deck_size", size)
    monkeypatch.setattr(radio, "pick_next", _picker([_pk(str(i)) for i in range(1, n + 1)]))
    s = radio.RadioSession()
    radio.start_dual_session(store, s, now=1.0)   # A=1,2,3 live
    return s


def test_on_play_advances_within_live_deck(monkeypatch, store):
    s = _started(monkeypatch, store)
    out = radio.on_play(store, s, "v2", now=2.0)
    assert out == {"foreign": False, "at_boundary": False, "standby_dirty": False}
    assert s.pos == 1 and "k1" in s.dispatched_keys


def test_on_play_flags_boundary_track(monkeypatch, store):
    s = _started(monkeypatch, store)
    out = radio.on_play(store, s, "v3", now=2.0)   # v3 is deck A's boundary
    assert out["at_boundary"] is True and out["foreign"] is False


def test_on_play_foreign_vid_at_boundary_is_s2_trigger(monkeypatch, store):
    s = _started(monkeypatch, store)
    radio.on_play(store, s, "v3", now=2.0)          # sit on the boundary track
    out = radio.on_play(store, s, "vAUTOPLAY", now=3.0)  # YTM autoplay leaked in after a skip-at-last
    assert out["foreign"] is True


def test_on_play_foreign_vid_mid_deck_is_inert(monkeypatch, store):
    # The corrected gate: a foreign vid is only an S2 toggle trigger when the session was already
    # sitting at the live deck's boundary track. Here we are still mid-deck (pos=0, not at boundary v3)
    # when a foreign vid shows up -- e.g. the user browsed away on their own. This must NOT signal a
    # toggle, and must NOT touch the queue or dispatched_keys (nothing new actually played).
    s = _started(monkeypatch, store)
    queue_before = list(s.decks["A"]["queue"])
    dispatched_before = set(s.dispatched_keys)
    pos_before = s.pos
    out = radio.on_play(store, s, "vSOMETHINGELSE", now=2.0)
    assert out == {"foreign": False, "at_boundary": False, "standby_dirty": False}
    assert s.decks["A"]["queue"] == queue_before
    assert s.dispatched_keys == dispatched_before
    assert s.pos == pos_before


def test_react_marks_standby_dirty_on_skip(monkeypatch, store):
    monkeypatch.setattr(store, "identity_key_for_video", lambda vid: "kold")
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: {"kold": {"artist": "Aold"}})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    s = _started(monkeypatch, store)
    s.standby_dirty = False
    radio.react(store, s, {"kind": "track_exit", "videoId": "vold",
                           "position": 5.0, "duration": 200.0}, now=4.0)   # ratio 0.025 -> skip
    assert s.standby_dirty is True and s.skips and s.skips[0][0] == "Aold"
