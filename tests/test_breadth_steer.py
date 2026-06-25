"""Interactive Breadth control (#7): the breadth bias tilts the feed's genre spread.

The bias in [-1, +1] reshapes how much each genre family contributes to the feed:
  +1 (eclectic) flattens toward uniform  -> rarer families surface more
  -1 (focused)  concentrates on dominant -> your top families dominate
   0 (neutral)  no change at all

These first tests pin the pure math kernel `_breadth_factors`; later tests wire it into
`_axis_weights_for` and the route/UI.
"""
from yt_playlist.rec import recommend, rec_params
from yt_playlist.util.matching import identity_key


def _stock(store):
    """A library dominated by Techno (8 tracks) with a rare Jazz tail (2). Returns the two candidate
    keys (one of each family) plus the identity so callers can steer breadth."""
    iid = store.upsert_identity("main", "cred", None, True)
    kt = kj = None
    for i in range(8):
        t = store.upsert_track(f"t{i}", f"T{i}", "DJ", None, None)
        store.set_track_genre(t, "Techno")
        kt = identity_key("T0", "DJ")
    for i in range(2):
        j = store.upsert_track(f"j{i}", f"J{i}", "Sax", None, None)
        store.set_track_genre(j, "Jazz")
        kj = identity_key("J0", "Sax")
    return iid, kt, kj


def test_neutral_bias_produces_no_tilt():
    """Center detent = exactly today's behavior: no per-family multipliers at all."""
    shares = {"Techno": 0.8, "Jazz": 0.2}
    assert recommend._breadth_factors(shares, bias=0.0, gain=1.0) == {}


def test_eclectic_boosts_rare_family_over_dominant():
    """Dragging toward eclectic favors your under-represented families and damps your dominant one."""
    shares = {"Techno": 0.8, "Jazz": 0.2}      # Techno dominant, Jazz rare
    f = recommend._breadth_factors(shares, bias=1.0, gain=1.0)
    assert f["Jazz"] > 1.0 > f["Techno"]       # rare lifted above neutral, dominant pushed below


def test_focused_boosts_dominant_family_over_rare():
    """Dragging toward focused is the mirror image: concentrate on your top families."""
    shares = {"Techno": 0.8, "Jazz": 0.2}
    f = recommend._breadth_factors(shares, bias=-1.0, gain=1.0)
    assert f["Techno"] > 1.0 > f["Jazz"]


def test_single_family_library_has_nothing_to_redistribute():
    """One family (or none) -> no spread to tilt -> empty factors regardless of bias."""
    assert recommend._breadth_factors({"Techno": 1.0}, bias=1.0, gain=1.0) == {}
    assert recommend._breadth_factors({}, bias=1.0, gain=1.0) == {}


def test_factors_are_clamped_to_a_sane_range():
    """A vanishingly rare family must not explode into an unbounded multiplier."""
    shares = {"Pop": 0.999, "Opera": 0.001}    # Opera ~ 0 share -> raw factor would blow up
    f = recommend._breadth_factors(shares, bias=1.0, gain=1.0)
    assert f["Opera"] <= 4.0                    # clamped at the ceiling


# --- wiring into the live re-rank path (_axis_weights_for) -------------------------------------


def test_eclectic_bias_ranks_rare_family_above_dominant(store):
    """End-to-end through the real re-rank hook: eclectic lifts a Jazz track over a Techno track."""
    _iid, kt, kj = _stock(store)
    rec_params.set_param(store, "breadth_bias", 1.0)
    mult = recommend._axis_weights_for(store, [kt, kj], now=10.0)
    assert mult is not None and mult[kj] > mult[kt]


def test_focused_bias_ranks_dominant_family_above_rare(store):
    """The mirror: focused lifts Techno (dominant) over Jazz (rare)."""
    _iid, kt, kj = _stock(store)
    rec_params.set_param(store, "breadth_bias", -1.0)
    mult = recommend._axis_weights_for(store, [kt, kj], now=10.0)
    assert mult is not None and mult[kt] > mult[kj]


def test_breadth_bias_keeps_overlay_alive_when_weights_neutral(store):
    """A set breadth bias must defeat the all-neutral early-return, or the dial would do nothing on a
    fresh model with no genre sliders touched."""
    _iid, kt, kj = _stock(store)
    rec_params.set_param(store, "breadth_bias", 1.0)
    assert recommend._axis_weights_for(store, [kt, kj], now=10.0) is not None


def test_neutral_breadth_bias_changes_nothing(store):
    """Default (0) bias keeps today's behavior: no weights/leans -> still None (no needless work)."""
    _iid, kt, kj = _stock(store)
    assert recommend._axis_weights_for(store, [kt, kj], now=10.0) is None
