# tests/test_axis_weight_cap.py
"""How hard your stated preferences bind (the axis_weight_cap knob).

A track's score is percentile(taste fit) x preference multiplier. The percentile is in (0, 1]; the
multiplier is four axis weights multiplied together and reaches 16. Uncapped, preference doesn't tilt
the ranking, it replaces it - and because only a track with a KNOWN genre or era can earn a
multiplier, a part-tagged library can never show its untagged half. Measured on a 30%-tagged library,
the Catalog surface's top 60 came back 100% tagged.
"""
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params
from yt_playlist.rec.scoring import AXIS_WEIGHT_UNBOUNDED, axis_adjusted_scores


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


# Two tracks: a superb fit with no preference behind it, and a mediocre fit in a favored genre.
_SCORES = {"great_fit": 1.0, "mediocre_fit": 0.5, "filler": 0.1}
_MULT = {"great_fit": 1.0, "mediocre_fit": 3.0, "filler": 1.0}


def _ranked(cap):
    out = axis_adjusted_scores(dict(_SCORES), dict(_MULT), cap=cap)
    return sorted(out, key=lambda k: -out[k])


def test_the_default_is_a_bounded_tilt():
    """#114 preferences steer taste fit by default rather than replacing it."""
    assert AXIS_WEIGHT_UNBOUNDED == rec_params.GENRE_MAX ** 4
    assert rec_params.PARAMS_BY_NAME["axis_weight_cap"].default == 1.4
    assert axis_adjusted_scores(dict(_SCORES), dict(_MULT)) == \
           axis_adjusted_scores(dict(_SCORES), dict(_MULT), cap=AXIS_WEIGHT_UNBOUNDED)


def test_uncapped_preference_outranks_a_better_fitting_track():
    """Today's behaviour, pinned: a 3x multiplier beats any taste fit at all."""
    assert _ranked(AXIS_WEIGHT_UNBOUNDED)[0] == "mediocre_fit"


def test_capping_lets_the_better_fit_win_again():
    """The point of the knob: preference becomes a strong tilt, not a gate."""
    assert _ranked(1.4)[0] == "great_fit"
    # ...but it still tilts: the favored track keeps its lead over an equally-unfavored worse fit.
    ranked = _ranked(1.4)
    assert ranked.index("mediocre_fit") < ranked.index("filler")


def test_a_muted_genre_stays_excluded_at_any_cap():
    """A mute is weight 0 and must remain a hard exclusion - the cap bounds the top only, so nothing
    can lift a muted track back into contention."""
    for cap in (1.0, 1.4, AXIS_WEIGHT_UNBOUNDED):
        out = axis_adjusted_scores({"muted": 1.0, "ok": 0.5}, {"muted": 0.0, "ok": 1.0}, cap=cap)
        assert out["muted"] == 0.0
        assert out["ok"] > 0.0


def test_a_de_emphasis_still_bites():
    """Only the upside is bounded: turning a genre down keeps working exactly as it did."""
    out = axis_adjusted_scores({"a": 1.0, "b": 1.0}, {"a": 0.25, "b": 1.0}, cap=1.4)
    assert out["a"] < out["b"]


@pytest.mark.parametrize("cap", [1.0, 1.4, 2.0, AXIS_WEIGHT_UNBOUNDED])
def test_the_knob_is_in_range_and_never_reorders_equals(cap):
    spec = rec_params.PARAMS_BY_NAME["axis_weight_cap"]
    assert spec.min <= cap <= spec.max
    out = axis_adjusted_scores({"a": 0.6, "b": 0.6}, {"a": 2.0, "b": 2.0}, cap=cap)
    assert out["a"] == out["b"]


def test_the_store_wired_knob_defaults_to_bounded_and_preserves_explicit_override():
    s = _store()
    assert rec_params.get_param(s, "axis_weight_cap") == 1.4
    rec_params.set_param(s, "axis_weight_cap", AXIS_WEIGHT_UNBOUNDED)
    assert rec_params.get_param(s, "axis_weight_cap") == AXIS_WEIGHT_UNBOUNDED
