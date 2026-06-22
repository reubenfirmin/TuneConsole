from yt_playlist.library.merge_order import merge_order, track_positions


def test_single_sequence_is_unchanged():
    assert merge_order([["a", "b", "c"]]) == ["a", "b", "c"]


def test_dedupes_fully_shared():
    assert merge_order([["a", "b"], ["a", "b"]]) == ["a", "b"]


def test_all_ids_present_exactly_once():
    seqs = [["a", "b", "c", "d"], ["b", "e", "d"], ["a", "e", "f"]]
    out = merge_order(seqs)
    assert sorted(out) == ["a", "b", "c", "d", "e", "f"]


def test_shared_track_lands_at_average_position():
    # 's' is last in A (pos 1.0) and first in B (pos 0.0) -> avg 0.5, between the extremes.
    pos = track_positions([["a", "s"], ["s", "b"]])
    assert pos["a"] == 0.0      # first in A
    assert pos["b"] == 1.0      # last in B
    assert pos["s"] == 0.5      # last in A (1.0) + first in B (0.0) -> 0.5


def test_unique_tracks_weave_around_shared_block():
    # The real-world repro: A is exactly the shared tail of B. B keeps its unique tracks
    # in a leading block. Positional merge must WEAVE them, not keep B-only segregated.
    shared = [f"s{i}" for i in range(13)]
    b_only = [f"b{i}" for i in range(13)]
    A = list(shared)
    B = b_only + shared                       # 13 unique, then the 13 shared (as in B)
    out = merge_order([A, B])
    # shared tracks must be interleaved among the b-only tracks, not all after them
    first_shared = min(out.index(s) for s in shared)
    last_b = max(out.index(b) for b in b_only)
    assert first_shared < last_b, f"expected weaving, got {out}"


def test_positions_normalized_0_to_1():
    pos = track_positions([["a", "b", "c"]])
    assert pos == {"a": 0.0, "b": 0.5, "c": 1.0}
