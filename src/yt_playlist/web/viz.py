"""Shared server-rendered SVG helpers for the Trends and Enrichment pages. No client charting: every
chart is an SVG path string plus invisible hover bands (each band feeds the shared [data-tip] tooltip).
Generalized from the original enrich.py._spark so both pages draw the same way."""
from datetime import datetime

SPARK_W, SPARK_H, SPARK_PAD = 520, 90, 4


def _bands(points, xs, w, pad, key="n"):
    """Full-height invisible hover bands, one per point, each tied to its value + formatted date.
    Prefers an explicit `label` on the point (Trends: proper UTC week/month labels, since those points
    are day/week/month-anchored and the old local-tz %H:%M fallback always rendered a stray 00:00).
    Falls back to the original local-tz per-play timestamp format when no label is given, so the
    Enrichment sparkline (real per-play timestamps) stays byte-identical."""
    out = []
    for i, p in enumerate(points):
        left = pad if i == 0 else (xs[i - 1] + xs[i]) / 2
        right = (w - pad) if i == len(points) - 1 else (xs[i] + xs[i + 1]) / 2
        lbl = p.get("label")
        if lbl is None:
            lbl = datetime.fromtimestamp(p["t"]).strftime("%b %-d, %H:%M")   # enrich: real per-play ts
        out.append({"x": round(left, 1), "w": round(max(right - left, 0.1), 1),
                    "label": lbl, "n": p[key]})
    return out


def area_spark(points, w=SPARK_W, h=SPARK_H, pad=SPARK_PAD):
    """Cumulative area sparkline (the shipped _spark, generalized). points = [{t, n}]. Scales n against
    the data max. Returns {path, bands}; empty path for fewer than 2 points."""
    if len(points) < 2:
        return {"path": "", "bands": []}
    ts = [p["t"] for p in points]
    t0, t1 = ts[0], ts[-1]
    nmax = max(p["n"] for p in points) or 1
    span = (t1 - t0) or 1
    def x(t):
        return round(pad + (w - 2 * pad) * (t - t0) / span, 1)
    def y(n):
        return round(h - pad - (h - 2 * pad) * n / nmax, 1)
    line = " ".join(f"L{x(p['t'])},{y(p['n'])}" for p in points)
    path = f"M{x(t0)},{h - pad} {line} L{x(t1)},{h - pad} Z"
    xs = [x(p["t"]) for p in points]
    return {"path": path, "bands": _bands(points, xs, w, pad, key="n")}


def line_path(points, ymax, w=SPARK_W, h=SPARK_H, pad=SPARK_PAD):
    """Open polyline for a rate/index chart. points = [{t, v}]; v scaled against a FIXED ymax (e.g. 1.0
    for a 0..1 rate) so the y axis is stable across renders. Returns {path, bands}."""
    if len(points) < 2:
        return {"path": "", "bands": []}
    ts = [p["t"] for p in points]
    t0, t1 = ts[0], ts[-1]
    ymax = ymax or 1.0
    span = (t1 - t0) or 1
    def x(t):
        return round(pad + (w - 2 * pad) * (t - t0) / span, 1)
    def y(v):
        return round(h - pad - (h - 2 * pad) * min(v, ymax) / ymax, 1)
    pts = " ".join(f"{'M' if i == 0 else 'L'}{x(p['t'])},{y(p['v'])}" for i, p in enumerate(points))
    xs = [x(p["t"]) for p in points]
    return {"path": pts, "bands": _bands(points, xs, w, pad, key="v")}


def stacked_areas(series, w=SPARK_W, h=SPARK_H, pad=SPARK_PAD):
    """Stacked share bands for the genre-family ribbon. series = [{key, points:[{t, v}]}], each t's v
    across series are shares (<= 1 total). Returns [{key, path}] bottom-up: each band fills between its
    running cumulative baseline and baseline+v. Shares are drawn on a fixed 0..1 axis."""
    if not series or len(series[0]["points"]) < 2:
        return []
    ts = [p["t"] for p in series[0]["points"]]
    t0, t1 = ts[0], ts[-1]
    span = (t1 - t0) or 1
    def x(t):
        return round(pad + (w - 2 * pad) * (t - t0) / span, 1)
    def y(v):
        return round(h - pad - (h - 2 * pad) * min(v, 1.0), 1)
    n = len(ts)
    cum = [0.0] * n
    out = []
    for s in series:
        pts = s["points"]
        top = [cum[i] + pts[i]["v"] for i in range(n)]
        upper = " ".join(f"{'M' if i == 0 else 'L'}{x(pts[i]['t'])},{y(top[i])}" for i in range(n))
        lower = " ".join(f"L{x(pts[i]['t'])},{y(cum[i])}" for i in range(n - 1, -1, -1))
        out.append({"key": s["key"], "path": f"{upper} {lower} Z"})
        cum = top
    return out


def stacked_top(series, w=SPARK_W, h=SPARK_H, pad=SPARK_PAD):
    """Open polyline along the TOP of stacked_areas' bands (the running cumulative total across every
    series at each t). Used to draw a single glowing "second trace" over the family-share wash, so a
    multi-band stack still reads as one instrument-style comparison line instead of needing its own
    per-band glow. Same x/y mapping as stacked_areas (fixed 0..1 share axis). Empty string for fewer
    than 2 points."""
    if not series or len(series[0]["points"]) < 2:
        return ""
    ts = [p["t"] for p in series[0]["points"]]
    t0, t1 = ts[0], ts[-1]
    span = (t1 - t0) or 1
    def x(t):
        return round(pad + (w - 2 * pad) * (t - t0) / span, 1)
    def y(v):
        return round(h - pad - (h - 2 * pad) * min(v, 1.0), 1)
    n = len(ts)
    cum = [0.0] * n
    for s in series:
        pts = s["points"]
        cum = [cum[i] + pts[i]["v"] for i in range(n)]
    return " ".join(f"{'M' if i == 0 else 'L'}{x(ts[i])},{y(cum[i])}" for i in range(n))


def line_area(points, ymax, w=SPARK_W, h=SPARK_H, pad=SPARK_PAD):
    """Filled area for a rate/index series on a FIXED 0..ymax axis (unlike area_spark, which scales to
    the data's own max) -- for charts like Discovery rate / Genre diversity, where the axis must stay
    stable across renders rather than rescaling to whatever the current data happens to span.

    Returns {path, stroke, bands}: `path` is the area closed down to a fixed bottom baseline (h - pad),
    so the shape sits on that baseline and rises upward as values increase; `stroke` is the same points
    as an open polyline (identical to line_path's own output) for drawing a crisp top edge over the
    area fill. Empty path/stroke/[] for fewer than 2 points.

    y(v) = h - pad - (h - 2*pad) * v/ymax. Hand-verified non-inversion: for v1 < v2,
    y(v1) - y(v2) = (h - 2*pad) * (v2 - v1) / ymax > 0, i.e. a LARGER v always produces a SMALLER y
    (SVG y grows downward toward the bottom of the viewport) -- so a monotonically increasing series
    of values produces monotonically DECREASING y coordinates along the stroke. This is what fixes the
    old "icicles hanging from the top" bug: that path was an open polyline with no baseline and no
    `fill: none`, so the browser's default black fill closed it with a straight diagonal from the last
    point back to the first and filled whatever that enclosed, instead of a proper area on a baseline."""
    if len(points) < 2:
        return {"path": "", "stroke": "", "bands": []}
    ts = [p["t"] for p in points]
    t0, t1 = ts[0], ts[-1]
    ymax = ymax or 1.0
    span = (t1 - t0) or 1
    def x(t):
        return round(pad + (w - 2 * pad) * (t - t0) / span, 1)
    def y(v):
        return round(h - pad - (h - 2 * pad) * min(v, ymax) / ymax, 1)
    stroke = " ".join(f"{'M' if i == 0 else 'L'}{x(p['t'])},{y(p['v'])}" for i, p in enumerate(points))
    line = " ".join(f"L{x(p['t'])},{y(p['v'])}" for p in points)
    path = f"M{x(t0)},{h - pad} {line} L{x(t1)},{h - pad} Z"
    xs = [x(p["t"]) for p in points]
    return {"path": path, "stroke": stroke, "bands": _bands(points, xs, w, pad, key="v")}
