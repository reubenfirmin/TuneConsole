"""Tools › Enrichment: corpus coverage charts + worker state/pause, all served from the store's
enrichment stats. The page polls /enrich/stats so the bars advance live as the worker drains."""
from datetime import datetime

from fastapi import APIRouter, Request

_SPARK_W, _SPARK_H, _SPARK_PAD = 520, 90, 4


def _spark(points):
    """Cumulative processed-over-time trend for the sparkline: an SVG area-path plus per-point hover
    bands (each carries a formatted date + count for the shared [data-tip] tooltip). `points` is
    [{t, n}] from store.processed_timeline(). Returns {path, bands} ({} path when too few points)."""
    w, h, pad = _SPARK_W, _SPARK_H, _SPARK_PAD
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
    path = f"M{x(t0)},{h - pad} {line} L{x(t1)},{h - pad} Z"   # area: baseline→curve→baseline→close
    xs = [x(p["t"]) for p in points]
    bands = []
    for i, p in enumerate(points):     # full-height invisible bands, each tied to its point
        left = pad if i == 0 else (xs[i - 1] + xs[i]) / 2
        right = (w - pad) if i == len(points) - 1 else (xs[i] + xs[i + 1]) / 2
        bands.append({"x": round(left, 1), "w": round(max(right - left, 0.1), 1),
                      "label": datetime.fromtimestamp(p["t"]).strftime("%b %-d, %H:%M"), "n": p["n"]})
    return {"path": path, "bands": bands}


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _ctx():
        cov = store.coverage_stats()
        total = cov["total"]
        def pct(k):
            return round(100 * cov[k] / total) if total else 0
        remaining = store.queue_remaining()
        enabled = store.get_setting("enrich_worker_enabled", "1") == "1"
        busy = bool(ctx.enrich_worker and ctx.enrich_worker.busy)
        if not enabled:
            state = "paused"
        elif busy or remaining > 0:
            state = "running"
        else:
            state = "idle"
        return {
            "cov": cov,
            "pct": {k: pct(k) for k in
                    ("processed", "genre", "year", "bpm", "energy", "danceability")},
            "remaining": remaining, "conflicts": store.outstanding_conflicts(),
            "enabled": enabled, "state": state,
            "spark": _spark(store.processed_timeline()),
        }

    @router.get("/enrich")
    def enrich_page(request: Request):
        return templates.TemplateResponse(request, "enrich.html", _ctx())

    @router.get("/enrich/stats")
    def enrich_stats(request: Request):
        return templates.TemplateResponse(request, "_partials/enrich_stats.html", _ctx())

    @router.post("/enrich/toggle")
    def enrich_toggle(request: Request):
        was_on = store.get_setting("enrich_worker_enabled", "1") == "1"
        store.set_setting("enrich_worker_enabled", "0" if was_on else "1")
        if was_on is False and ctx.enrich_worker:     # just turned ON -> wake the drain loop
            ctx.enrich_worker.trigger()
        return templates.TemplateResponse(request, "_partials/enrich_stats.html", _ctx())

    return router
