import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import radio, rec_params


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    # #93 pin variety to 0: this whole file asserts EXACT pick order out of real (unmocked) pick_next,
    # which is the pre-sampling ranking these tests exist to pin. Sampling is layered above that ranking
    # (radio_variety > 0 draws among the top candidates instead of always taking rank 0), so pinning 0
    # here keeps every assertion meaningful without asserting anything about the sampler itself (that
    # lives in test_radio_queue.py's dedicated variety tests).
    rec_params.set_param(s, "radio_variety", 0)
    return s


def _meta(keys):
    # distinct artist per key by default; the picker reads video_id + artist + title.
    return {k: {"video_id": "v" + k, "title": "T" + k, "artist": "art_" + k,
                "album": "", "thumbnail": None, "plays": 0} for k in keys}


def test_skip_penalty_arithmetic():
    # params: artist_pen=0.5, mode_pen=0.25, halflife_h=2.0.  One skip 1h ago on artist "Foo", mode 3.
    #   age = 3600s ; halflife_days = 2/24 ; decay = 0.5 ** (3600 / ((2/24)*86400))
    #   (2/24)*86400 = 7200 ; 3600/7200 = 0.5 ; decay = 0.5 ** 0.5 = 0.70710678...
    params = {"artist_pen": 0.5, "mode_pen": 0.25, "halflife_h": 2.0}
    s = radio.RadioSession()
    s.skips = [("Foo", 3, 1000.0 - 3600.0)]
    now = 1000.0
    # same artist + same mode -> (0.5 + 0.25) * 0.70710678 = 0.75 * 0.70710678 = 0.53033009
    assert radio.skip_penalty("Foo", 3, s, now, params) == pytest.approx(0.53033009, abs=1e-6)
    # same artist, other mode -> 0.5 * 0.70710678 = 0.35355339
    assert radio.skip_penalty("Foo", 9, s, now, params) == pytest.approx(0.35355339, abs=1e-6)
    # other artist, same mode -> 0.25 * 0.70710678 = 0.17677670
    assert radio.skip_penalty("Bar", 3, s, now, params) == pytest.approx(0.17677670, abs=1e-6)
    # neither -> 0.0
    assert radio.skip_penalty("Bar", 9, s, now, params) == 0.0
    # a fresh skip (age 0) does not decay: decay_weight(0,..) == 1.0 -> 0.75 * 1.0 = 0.75
    s.skips = [("Foo", 3, now)]
    assert radio.skip_penalty("Foo", 3, s, now, params) == pytest.approx(0.75, abs=1e-9)


def test_pick_next_picks_top_eligible(monkeypatch, store):
    monkeypatch.setattr(radio, "_score_map", lambda st, now: {"a": 1.0, "b": 0.9, "c": 0.8})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)   # no modes -> mode penalty inert
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    s = radio.RadioSession(); s.active = True
    pick = radio.pick_next(store, s, now=10.0)
    assert pick["key"] == "a"
    assert pick["video_id"] == "va"
    assert pick["url"] == "https://music.youtube.com/watch?v=va"


def test_pick_next_skips_dispatched_and_primed(monkeypatch, store):
    monkeypatch.setattr(radio, "_score_map", lambda st, now: {"a": 1.0, "b": 0.9, "c": 0.8})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    s = radio.RadioSession(); s.active = True
    s.dispatched_keys = {"a"}
    s.primed = {"key": "b"}
    assert radio.pick_next(store, s, now=10.0)["key"] == "c"   # a played, b primed -> c


def test_pick_next_honors_artist_cap(monkeypatch, store):
    # a and b share one artist; cap 1 means once a's artist is counted, b is excluded -> c wins.
    meta = {"a": {"video_id": "va", "title": "Ta", "artist": "same"},
            "b": {"video_id": "vb", "title": "Tb", "artist": "same"},
            "c": {"video_id": "vc", "title": "Tc", "artist": "other"}}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: {"a": 1.0, "b": 0.9, "c": 0.8})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: meta)
    rec_params.set_param(store, "radio_artist_cap", 1)
    s = radio.RadioSession(); s.active = True
    s.artist_counts = {"same": 1}                              # artist already at cap
    assert radio.pick_next(store, s, now=10.0)["key"] == "c"


def test_pick_next_reaches_past_exhausted_pool(monkeypatch, store):
    # pool=2 but 5 candidates exist. Once the top-2 pool has been fully committed into the queue, a
    # 3rd pick must still come from the remaining catalog rather than returning None forever.
    # radio_candidate_pool's ParamSpec has min=10, so rec_params.set_param(store, ..., 2) would
    # silently clamp to 10 and never truncate a 5-candidate pool. Patch the _params() seam directly
    # (same style as the _score_map/_modeinfo monkeypatches above) so pool=2 is real.
    #
    # v2 contract: note_dispatch only tallies the artist cap; a pick is excluded from later picks by
    # `_exclusions` (queue + dispatched_keys + primed), not by note_dispatch alone. So committing a
    # pick into the queue (what `_pick_tail` does for every real caller) is what must survive this
    # property: exclusions apply BEFORE the pool is truncated, so the pool being fully consumed does
    # not stop the session, only the whole catalog being exhausted does.
    scores = {"a": 1.0, "b": 0.9, "c": 0.8, "d": 0.7, "e": 0.6}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: scores)
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    monkeypatch.setattr(radio, "_params", lambda st: {
        "artist_cap": 10, "pool": 2, "artist_pen": 0.5, "mode_pen": 0.25,
        "halflife_h": 2.0, "volume_floor": 0.1, "variety": 0,
    })
    s = radio.RadioSession(); s.active = True
    p1 = radio.pick_next(store, s, now=10.0)
    assert p1["key"] == "a"
    radio.note_dispatch(s, p1)
    s.queue.append(p1)   # commit into the queue, exactly like _pick_tail does for every real caller
    p2 = radio.pick_next(store, s, now=10.0)
    assert p2["key"] == "b"
    radio.note_dispatch(s, p2)
    s.queue.append(p2)
    # top-2 pool (a, b) is now fully committed; the old bug would return None here forever.
    p3 = radio.pick_next(store, s, now=10.0)
    assert p3 is not None
    assert p3["key"] == "c"


def test_pick_next_none_when_catalog_exhausted(monkeypatch, store):
    scores = {"a": 1.0, "b": 0.9}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: scores)
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    s = radio.RadioSession(); s.active = True
    s.dispatched_keys = {"a", "b"}   # every candidate already played
    assert radio.pick_next(store, s, now=10.0) is None


def test_pick_next_excludes_missing_video_id(monkeypatch, store):
    # "a" scores highest but has no video_id -> must be skipped, not just cause a pool slot to be
    # wasted; "b" (real video_id) should win.
    meta = {"a": {"video_id": None, "title": "Ta", "artist": "art_a"},
            "b": {"video_id": "vb", "title": "Tb", "artist": "art_b"}}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: {"a": 1.0, "b": 0.9})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: meta)
    s = radio.RadioSession(); s.active = True
    assert radio.pick_next(store, s, now=10.0)["key"] == "b"


def test_pick_next_none_without_vectors(monkeypatch, store):
    monkeypatch.setattr(radio, "_score_map", lambda st, now: None)
    s = radio.RadioSession(); s.active = True
    assert radio.pick_next(store, s, now=10.0) is None


def test_note_dispatch_updates_session():
    # v2 contract: note_dispatch only tallies the artist cap for a pick committed into the queue.
    # Being PLAYED (dispatched_keys) is folded later by on_play when pos advances past it, not here.
    s = radio.RadioSession()
    radio.note_dispatch(s, {"key": "a", "video_id": "va", "artist": "art"})
    assert s.artist_counts == {"art": 1}
    assert s.dispatched_keys == set()
    assert s.dispatched_vids == set()


def test_tilt_mult_arithmetic():
    # GENRE_MIN, GENRE_MAX = 0.0, 2.0 (rec_params). tilts favor House genre (1.5), damp the 1990s (0.5).
    tilts = {"genre:House": 1.5, "era:1990": 0.5}
    # X: House / 1990s / artist DJ -> gt=1.5, et=0.5, at=1.0 -> 1.5*0.5*1.0 = 0.75 (no clamp needed)
    assert radio._tilt_mult(("House", None, "1990", "DJ"), tilts) == pytest.approx(0.75)
    # Z: House / 2010s -> gt=1.5, et=1.0 -> 1.5
    assert radio._tilt_mult(("House", None, "2010", "DJ2"), tilts) == pytest.approx(1.5)
    # Y: Techno / 2000s -> all neutral -> 1.0
    assert radio._tilt_mult(("Techno", None, "2000", "DJ3"), tilts) == pytest.approx(1.0)
    # empty tilts -> neutral
    assert radio._tilt_mult(("House", None, "1990", "DJ"), {}) == pytest.approx(1.0)


def test_pick_next_applies_tilt(monkeypatch, store):
    # Raw scores: X=1.0 (top), Y=0.9, Z=0.6. Tilt: era:1990 halves X, genre:House lifts Z (1.5).
    #   adjusted(X) = 1.0 * 0.75 = 0.75 ; adjusted(Y) = 0.9 * 1.0 = 0.9 ; adjusted(Z) = 0.6 * 1.5 = 0.9
    #   Y and Z tie at 0.9, X drops to 0.75. Iteration keeps the FIRST max (Y, seen before Z by score).
    monkeypatch.setattr(radio, "_score_map", lambda st, now: {"x": 1.0, "y": 0.9, "z": 0.6})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(radio, "_axis_info", lambda st, keys: {
        "x": ("House", None, "1990", "DJ"), "y": ("Techno", None, "2000", "DJ2"),
        "z": ("House", None, "2010", "DJ3")})
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: {
        k: {"video_id": "v" + k, "title": "T" + k, "artist": "a" + k} for k in keys})
    s = radio.RadioSession(); s.active = True
    s.tilts = {"genre:House": 1.5, "era:1990": 0.5}
    assert radio.pick_next(store, s, now=10.0)["key"] == "y"   # X demoted below the Y/Z tie; Y wins ties


def test_reset_clears_tilts_by_default():
    s = radio.RadioSession()
    s.tilts = {"genre:House": 1.5}
    s.reset()
    assert s.tilts == {}


def test_reset_keep_tilts_preserves_tilts():
    s = radio.RadioSession()
    s.tilts = {"genre:House": 1.5}
    s.dispatched_keys = {"a"}
    s.reset(keep_tilts=True)
    assert s.tilts == {"genre:House": 1.5}
    assert s.dispatched_keys == set()   # everything else still clears


def test_pick_next_no_tilt_skips_axis_info(monkeypatch, store):
    # When session.tilts is empty, _axis_info must not be called at all (neutral no-op fast path).
    def _boom(st, keys):
        raise AssertionError("_axis_info should not be called when tilts is empty")
    monkeypatch.setattr(radio, "_score_map", lambda st, now: {"a": 1.0, "b": 0.9})
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(radio, "_axis_info", _boom)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    s = radio.RadioSession(); s.active = True
    assert radio.pick_next(store, s, now=10.0)["key"] == "a"
