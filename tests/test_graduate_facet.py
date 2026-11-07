import pytest

from yt_playlist.rec import graduation, rec_params


def test_graduate_facet_nudges_weight_once_threshold_crossed(store):
    axis = "journey:energy_arc"
    # Each +1 accumulates in the theme ledger; crossing the threshold nudges the permanent weight.
    n = int(rec_params.get_param(store, "theme_threshold")) + 1
    for _ in range(n):
        graduation.graduate_facet(store, axis, 1.0, 1.0)
    # #85 read at the same `now` as the nudges, else read-time reversion (vs real wall-clock time)
    # would erase it before the assertion runs.
    assert store.rec_model.get_weights(now=1.0)[axis] == pytest.approx(1.05)
    rec_params.set_param(store, "theme_threshold", 2.5)
    rec_params.set_param(store, "graduate_up", 1.2)
    overridden = "journey:overridden"
    for _ in range(2):
        graduation.graduate_facet(store, overridden, 1.0, 1.0)
    assert store.rec_model.get_weights(now=1.0).get(overridden, 1.0) == 1.0
    graduation.graduate_facet(store, overridden, 1.0, 1.0)
    assert store.rec_model.get_weights(now=1.0)[overridden] == pytest.approx(1.2)
    assert store.rec_signals.get_theme(overridden) == pytest.approx(.5)
    rec_params.reset_param(store, "theme_threshold")
    rec_params.reset_param(store, "graduate_up")
    reset = "journey:reset"
    for _ in range(n):
        graduation.graduate_facet(store, reset, 1.0, 1.0)
    assert store.rec_model.get_weights(now=1.0)[reset] == pytest.approx(1.05)


def test_graduate_facet_negative_lowers_weight(store):
    axis = "journey:odyssey"
    n = int(rec_params.get_param(store, "theme_threshold")) + 1
    rec_params.set_param(store, "graduate_down", .8)
    for _ in range(n):
        graduation.graduate_facet(store, axis, -1.0, 1.0)
    # #85 same `now` as the nudges (see test above)
    assert store.rec_model.get_weights(now=1.0)[axis] == pytest.approx(.8)
