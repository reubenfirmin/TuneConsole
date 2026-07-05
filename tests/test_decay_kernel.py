"""#85 the wall-clock decay kernel: one half-life halves an event's weight."""
import pytest

from yt_playlist.rec import rec_params
from yt_playlist.rec.transient import decay_weight


def test_fresh_and_future_events_are_full_weight():
    assert decay_weight(0.0, 3.0) == 1.0
    assert decay_weight(-500.0, 3.0) == 1.0


def test_one_half_life_halves():
    assert decay_weight(3 * 86400.0, 3.0) == pytest.approx(0.5)
    assert decay_weight(14 * 86400.0, 7.0) == pytest.approx(0.25)


def test_halflife_params_registered_with_defaults():
    by_name = rec_params.PARAMS_BY_NAME
    for name, default in [("play_halflife_d", 3), ("mood_halflife_d", 7),
                          ("like_halflife_d", 21), ("dislike_halflife_d", 45),
                          ("weight_revert_halflife_d", 60)]:
        assert name in by_name and by_name[name].default == default


def test_rank_decay_params_are_gone():
    assert "mood_recency_alpha" not in rec_params.PARAMS_BY_NAME
    assert "stale_decay_halflife_d" not in rec_params.PARAMS_BY_NAME
    assert not hasattr(rec_params, "MOOD_RECENCY_ALPHA")
    assert not hasattr(rec_params, "STALE_DECAY_HALFLIFE_D")
