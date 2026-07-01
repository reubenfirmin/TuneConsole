import numpy as np
from yt_playlist.rec import mode_surfaces as ms


def _mode(mid, vec, size, fams):
    return {"mode_id": mid, "centroid": np.asarray(vec, dtype=np.float32),
            "size": size, "families": fams}


def _modes():
    return [
        _mode(1, [1, 0, 0], 100, [("house", 100)]),
        _mode(2, [0, 1, 0], 100, [("techno", 100)]),
        _mode(3, [-1, 0, 0], 100, [("rock-indie", 100)]),
        _mode(4, [0, -1, 0], 100, [("trance", 100)]),
        _mode(5, [0, 0, 1], 100, [("jazz", 100)]),
    ]


def test_select_returns_n_distinct():
    got = ms.select_modes(None, _modes(), leans={}, epoch=0, n=4)
    assert len(got) == 4 and len(set(got)) == 4


def test_dominant_responds_to_leans():
    # The dominant is a weighted random draw, so a lean BIASES it, it doesn't guarantee it for one
    # epoch. A strong techno lean should make the techno mode (id 2) the most frequent dominant
    # across epochs, and far more frequent than with no lean.
    from collections import Counter
    strong = Counter(ms.select_modes(None, _modes(), leans={"genre:techno": 5.0}, epoch=e, n=4)[0]
                     for e in range(200))
    none = Counter(ms.select_modes(None, _modes(), leans={}, epoch=e, n=4)[0]
                   for e in range(200))
    assert strong.most_common(1)[0][0] == 2          # techno mode dominates the menu most often
    assert strong[2] > none[2]                        # the lean clearly lifts it vs no lean


def test_deterministic_for_fixed_inputs():
    a = ms.select_modes(None, _modes(), leans={}, epoch=7, n=4)
    b = ms.select_modes(None, _modes(), leans={}, epoch=7, n=4)
    assert a == b


def test_artist_cap_limits_per_artist():
    items = [{"artist": "Muse", "key": str(i)} for i in range(5)] + [{"artist": "U2", "key": "x"}]
    out = ms.artist_cap(items, max_per=2)
    assert sum(1 for d in out if d["artist"] == "Muse") == 2
    assert sum(1 for d in out if d["artist"] == "U2") == 1
    assert [d["key"] for d in out][:2] == ["0", "1"]
