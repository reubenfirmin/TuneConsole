import math

from yt_playlist.rec import rose


def test_empty_returns_empty():
    assert rose.rose_geometry([]) == []
    assert rose.rose_geometry_deviation([], scale=0.2) == []


def test_petal_count_and_angles():
    petals = rose.rose_geometry([1.0, 2.0, 3.0])
    assert len(petals) == 3
    mids = [p["mid_deg"] for p in petals]
    assert math.isclose((mids[1] - mids[0]) % 360, 120.0, abs_tol=1e-6)
    assert all(p["path"].startswith("M") for p in petals)


def test_frac_scales_to_max():
    petals = rose.rose_geometry([1.0, 2.0, 4.0])
    assert math.isclose(petals[2]["frac"], 1.0)
    assert math.isclose(petals[0]["frac"], 0.25)


def test_all_zero_no_crash():
    petals = rose.rose_geometry([0.0, 0.0])
    assert all(p["frac"] == 0.0 for p in petals)
    assert all(p["path"] for p in petals)


def test_deviation_zero_has_no_petal():
    # A genre whose recent share matches its all-time share has zero deviation -> it draws nothing
    # (sits on the baseline ring), so it can never look "bigger" than a small permanent slice.
    petals = rose.rose_geometry_deviation([0.0, 0.0], scale=0.2)
    assert all(p["path"] == "" for p in petals)
    assert all("neutral_r" in p for p in petals)


def test_deviation_sign_and_band():
    petals = rose.rose_geometry_deviation([0.1, -0.1, 0.0], scale=0.2)
    assert petals[0]["sign"] == 1 and petals[1]["sign"] == -1 and petals[2]["sign"] == 0
    assert petals[0]["path"] and petals[1]["path"]      # nonzero deviations draw a band
    assert petals[2]["path"] == ""                      # zero vanishes
    assert petals[0]["frac"] > 0 and petals[1]["frac"] < 0


def test_deviation_clamps_to_scale():
    petals = rose.rose_geometry_deviation([0.5], scale=0.2)   # 0.5/0.2 = 2.5 -> clamped to 1.0
    assert math.isclose(petals[0]["frac"], 1.0)
