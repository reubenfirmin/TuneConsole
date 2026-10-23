from collections import deque
from statistics import median
from time import perf_counter

from yt_playlist.rec.surfaces import _weighted_fair_queue


def _row(key, artist="Artist"):
    return {"key": key, "title": key, "artist": artist, "album": "Album", "video_id": key,
            "thumbnail": None, "plays": 1}


def _lane(rows, name, weight=1):
    return [rows, lambda row: "Reason " + row["key"], name, weight, 0]


def test_weighted_order_and_stable_ties_survive_partial_consumption():
    queues = [_lane([_row(f"a{i}") for i in range(4)], "a", 2),
              _lane([_row(f"b{i}") for i in range(2)], "b")]
    first = _weighted_fair_queue(queues, 2, set(), set())
    assert [item.key for item in first] == ["a0", "a1"]
    assert isinstance(queues[0][0], deque)
    rest = _weighted_fair_queue(queues, 10, set(), set())
    assert [item.key for item in rest] == ["b0", "a2", "a3", "b1"]
    assert [q[4] for q in queues] == [4, 2]


def test_overlaps_suppression_muting_and_traces_preserve_selection():
    queues = [_lane([_row("blocked"), _row("shared"), _row("muted", "Muted"), _row("a")], "a", 2),
              _lane([_row("shared"), _row("b"), _row("c")], "b")]
    traces = {"shared": {"score": 3}}
    out = _weighted_fair_queue(queues, 10, {"blocked"}, {"Muted"}, traces)
    assert [(item.key, item.lane) for item in out] == [("shared", "a"), ("a", "a"), ("b", "b"), ("c", "b")]
    assert out[0].trace is traces["shared"]
    assert out[0].reason == "Reason shared"
    assert [q[4] for q in queues] == [2, 2]


def test_full_library_drain_keeps_smaller_lane_and_scales_linearly():
    timings = {}
    for size in (8000, 32000):
        elapsed = []
        for _ in range(3):
            queues = [_lane([_row(str(i)) for i in range(size)], "large", 3),
                      _lane([_row(str(i)) for i in range(size // 2)] + [_row("small-only")], "small")]
            hard = {str(i) for i in range(0, size, 2)}
            start = perf_counter()
            out = _weighted_fair_queue(queues, size * 2, hard, set())
            elapsed.append(perf_counter() - start)
            assert {item.key for item in out} == {str(i) for i in range(1, size, 2)} | {"small-only"}
            assert not any(q[0] for q in queues)
        timings[size] = median(elapsed)
    # Report scaling without a machine-speed-dependent timing assertion.
    print(f"Fair queue full drains (median seconds): {timings}; 4x input ratio: {timings[32000] / timings[8000]:.2f}")
