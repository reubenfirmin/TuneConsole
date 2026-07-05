from yt_playlist.web import viz


def test_area_spark_matches_shipped_shape():
    pts = [{"t": 0.0, "n": 0}, {"t": 10.0, "n": 5}, {"t": 20.0, "n": 5}]
    out = viz.area_spark(pts, w=520, h=90, pad=4)
    # nmax = 5, span = 20. x(0)=4, x(10)=260.0, x(20)=516.0 (pad + 512*(t/20)).
    # y(0)=86.0 (baseline h-pad), y(5)=4.0 (top). Area path: M start-baseline, L each point, L end-baseline, Z.
    # Baseline coords (start/end) embed the raw int h-pad, so they render as "86" not "86.0".
    assert out["path"] == "M4.0,86 L4.0,86.0 L260.0,4.0 L516.0,4.0 L516.0,86 Z"
    assert len(out["bands"]) == 3


def test_area_spark_too_few_points():
    assert viz.area_spark([{"t": 0.0, "n": 1}]) == {"path": "", "bands": []}


def test_line_path_uses_fixed_ymax():
    pts = [{"t": 0.0, "v": 0.5}, {"t": 10.0, "v": 1.0}]
    out = viz.line_path(pts, ymax=1.0, w=520, h=90, pad=4)
    # x(0)=4, x(10)=516. y(0.5)= 86 - 82*0.5 = 45.0 ; y(1.0)= 86 - 82*1.0 = 4.0.
    assert out["path"] == "M4.0,45.0 L516.0,4.0"


def test_stacked_areas_stacks_bottom_up():
    series = [{"key": "house", "points": [{"t": 0.0, "v": 0.5}, {"t": 1.0, "v": 0.5}]},
              {"key": "techno", "points": [{"t": 0.0, "v": 0.5}, {"t": 1.0, "v": 0.5}]}]
    out = viz.stacked_areas(series, w=520, h=90, pad=4)
    assert [b["key"] for b in out] == ["house", "techno"]
    # house band spans cum 0.0 -> 0.5 ; techno band spans 0.5 -> 1.0 (stacked on top).
    assert "Z" in out[0]["path"] and "Z" in out[1]["path"]


def test_stacked_top_traces_the_cumulative_total():
    series = [{"key": "house", "points": [{"t": 0.0, "v": 0.2}, {"t": 1.0, "v": 0.3}]},
              {"key": "techno", "points": [{"t": 0.0, "v": 0.1}, {"t": 1.0, "v": 0.1}]}]
    out = viz.stacked_top(series, w=520, h=90, pad=4)
    # cumulative totals: t=0 -> 0.3 ; t=1 -> 0.4. y(0.3) = 86 - 82*0.3 = 61.4 ; y(0.4) = 86 - 82*0.4 = 53.2.
    assert out == "M4.0,61.4 L516.0,53.2"


def test_stacked_top_too_few_points():
    assert viz.stacked_top([{"key": "house", "points": [{"t": 0.0, "v": 0.2}]}]) == ""
    assert viz.stacked_top([]) == ""


def test_line_area_is_not_inverted_for_increasing_series():
    """The classic bug this guards against: an open polyline with no explicit fill gets closed by the
    browser's default black fill along a straight diagonal from its last point back to its first,
    producing icicle-like triangles instead of a proper area. line_area instead closes explicitly to a
    fixed bottom baseline, and its y-mapping must never invert: a bigger v always maps to a SMALLER y
    (SVG y grows downward), so a monotonically increasing series produces monotonically DECREASING y
    coordinates along the stroke -- verified below against a concrete rising series."""
    pts = [{"t": 0.0, "v": 0.1}, {"t": 10.0, "v": 0.5}, {"t": 20.0, "v": 0.9}]
    out = viz.line_area(pts, ymax=1.0, w=520, h=90, pad=4)
    ys = [float(seg.split(",")[1]) for seg in out["stroke"].replace("M", "L").split("L") if seg.strip()]
    assert ys == sorted(ys, reverse=True) and ys[0] > ys[-1]
    # the area closes to the fixed bottom baseline (h - pad = 86) at both ends, i.e. it "sits on the
    # bottom baseline rising upward" rather than hanging from the top.
    assert out["path"].startswith("M4.0,86 ")
    assert out["path"].rstrip("Z ").endswith(",86")


def test_line_area_stroke_matches_line_path():
    """line_area's crisp top edge must be pixel-identical to line_path's own polyline for the same
    inputs -- it's the same y-mapping, just also closed to a baseline for the fill."""
    pts = [{"t": 0.0, "v": 0.5}, {"t": 10.0, "v": 1.0}]
    la = viz.line_area(pts, ymax=1.0, w=520, h=90, pad=4)
    lp = viz.line_path(pts, ymax=1.0, w=520, h=90, pad=4)
    assert la["stroke"] == lp["path"]
    assert la["bands"] == lp["bands"]


def test_line_area_too_few_points():
    assert viz.line_area([{"t": 0.0, "v": 1}], ymax=1.0) == {"path": "", "stroke": "", "bands": []}


def test_bands_prefer_explicit_label():
    """Trends points carry their own UTC week/month label; _bands must use it verbatim instead of
    falling back to the tz-naive %H:%M format (which always renders 00:00 for day/week/month-anchored
    timestamps)."""
    out = viz.area_spark([{"t": 0.0, "n": 1, "label": "Week of Jan 1"},
                          {"t": 604800.0, "n": 2, "label": "Week of Jan 8"}])
    assert [b["label"] for b in out["bands"]] == ["Week of Jan 1", "Week of Jan 8"]


def test_bands_fallback_unchanged_for_enrich():
    # No label key -> byte-identical to the shipped local-tz "%b %-d, %H:%M" fallback (enrich path).
    out = viz.area_spark([{"t": 0.0, "n": 1}, {"t": 10.0, "n": 2}])
    assert ", " in out["bands"][0]["label"] and ":" in out["bands"][0]["label"]
