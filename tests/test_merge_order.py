from yt_playlist.library.merge_order import track_positions


def test_shared_track_lands_at_average_position():
    # 's' is last in A (pos 1.0) and first in B (pos 0.0) -> avg 0.5, between the extremes.
    pos = track_positions([["a", "s"], ["s", "b"]])
    assert pos["a"] == 0.0      # first in A
    assert pos["b"] == 1.0      # last in B
    assert pos["s"] == 0.5      # last in A (1.0) + first in B (0.0) -> 0.5


def test_positions_normalized_0_to_1():
    pos = track_positions([["a", "b", "c"]])
    assert pos == {"a": 0.0, "b": 0.5, "c": 1.0}
