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


def test_bands_prefer_explicit_label():
    """An explicit point label takes precedence over the timestamp fallback."""
    out = viz.area_spark([{"t": 0.0, "n": 1, "label": "Week of Jan 1"},
                          {"t": 604800.0, "n": 2, "label": "Week of Jan 8"}])
    assert [b["label"] for b in out["bands"]] == ["Week of Jan 1", "Week of Jan 8"]


def test_bands_fallback_unchanged_for_enrich():
    # No label key -> byte-identical to the shipped local-tz "%b %-d, %H:%M" fallback (enrich path).
    out = viz.area_spark([{"t": 0.0, "n": 1}, {"t": 10.0, "n": 2}])
    assert ", " in out["bands"][0]["label"] and ":" in out["bands"][0]["label"]


def test_area_spark_scales_to_series_maximum():
    small = viz.area_spark([{"t": 0, "n": 1}, {"t": 1, "n": 2}])
    large = viz.area_spark([{"t": 0, "n": 100}, {"t": 1, "n": 200}])
    assert small["path"] == large["path"]       # identical, despite a 100x difference


def test_comparison_line_uses_the_shared_scale_and_stays_open():
    out = viz.area_spark([{"t": 0, "n": 0}, {"t": 1, "n": 2}], maximum=10, filled=False)
    assert out["path"] == "M4.0,86.0 L516.0,69.6"
