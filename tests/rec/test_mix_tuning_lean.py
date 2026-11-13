"""Pure steer-bar math: target weight -> clamped lean. No store, no I/O."""
from yt_playlist.rec import mix_tuning, rec_params


def test_lean_divides_target_by_the_current_permanent_weight():
    assert mix_tuning.lean_for_target(2.0, 3.0) == 1.5


def test_lean_falls_back_to_the_target_when_permanent_weight_is_zero():
    assert mix_tuning.lean_for_target(0.0, 1.3) == 1.3


def test_lean_is_clamped_to_the_genre_lean_range():
    assert mix_tuning.lean_for_target(1.0, 100.0) == rec_params.GENRE_MAX
    assert mix_tuning.lean_for_target(1.0, -100.0) == rec_params.GENRE_MIN
