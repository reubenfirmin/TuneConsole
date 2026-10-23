"""Server-rendered SVG sparklines and tooltip hover bands for Enrichment."""
from datetime import datetime

SPARK_W, SPARK_H, SPARK_PAD = 520, 90, 4


def _bands(points, xs, w, pad, key="n"):
    """Full-height invisible hover bands, one per point, each tied to its value + formatted date.
    Uses an explicit `label` when supplied, otherwise the local-time per-play timestamp format."""
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


def area_spark(points, w=SPARK_W, h=SPARK_H, pad=SPARK_PAD, *, maximum=None, filled=True):
    """Cumulative area sparkline. points = [{t, n}]. Scales n against
    the data max. Returns {path, bands}; empty path for fewer than 2 points."""
    if len(points) < 2:
        return {"path": "", "bands": []}
    ts = [p["t"] for p in points]
    t0, t1 = ts[0], ts[-1]
    nmax = maximum or max(p["n"] for p in points) or 1
    span = (t1 - t0) or 1
    def x(t):
        return round(pad + (w - 2 * pad) * (t - t0) / span, 1)
    def y(n):
        return round(h - pad - (h - 2 * pad) * n / nmax, 1)
    line = " ".join(f"L{x(p['t'])},{y(p['n'])}" for p in points)
    path = (f"M{x(t0)},{h - pad} {line} L{x(t1)},{h - pad} Z" if filled else
            f"M{x(t0)},{y(points[0]['n'])} " + " ".join(
                f"L{x(p['t'])},{y(p['n'])}" for p in points[1:]))
    xs = [x(p["t"]) for p in points]
    return {"path": path, "bands": _bands(points, xs, w, pad, key="n")}
