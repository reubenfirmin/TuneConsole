"""Trends page (#76-#80): server-rendered SVG time-series read entirely from the precomputed
'trend_rollups' proposal. The rec worker builds the rollup; every handler here only reads it and lays
out SVG via web.viz. Expensive sections lazy-load with hx-trigger='load'."""
import datetime as _dt

from fastapi import APIRouter, Request

from yt_playlist.web import viz

DEAD_PLAYLISTS_CAP = 8

# Staleness bucket -> instrument-trace color, freshest to most-stale, "never" last (matches the order
# health_snapshot's _STALE_BUCKETS already emits). Colors read as a temperature ramp: teal (fresh) ->
# amber -> danger red (very stale) -> a neutral gray for "never played" (nothing to gauge staleness of).
_STALE_COLORS = {
    "played <30d": "var(--teal)",
    "30-90d": "var(--accent-2)",
    "90-365d": "#f6c177",
    ">365d": "var(--danger)",
    "never": "var(--border)",
}


def _week_day_label(day) -> str:
    """Short month label for a UTC day number (week_start_day), e.g. 'Feb'."""
    return _dt.datetime.fromtimestamp(day * 86400, tz=_dt.timezone.utc).strftime("%b")


def _month_label(month: str) -> str:
    """Short month label for a rollup 'YYYY-MM' string, e.g. 'Feb'. Parsed from the string directly
    (not the tz-naive timestamp some handlers build from it) so the label can't drift a day across a
    local/UTC boundary."""
    y, m = (int(x) for x in month.split("-"))
    return _dt.datetime(y, m, 1, tzinfo=_dt.timezone.utc).strftime("%b")


def _month_name(month: str) -> str:
    """Full month name for a rollup 'YYYY-MM' string, e.g. 'June' -- feeds the Month in review
    "Your June" wrapped-style headline (#79)."""
    y, m = (int(x) for x in month.split("-"))
    return _dt.datetime(y, m, 1, tzinfo=_dt.timezone.utc).strftime("%B")


def _week_tip(w) -> str:
    """Hover-band label for a week-anchored point, e.g. 'Week of Feb 3' -- parsed from the UTC
    week_start_day, not a tz-naive fromtimestamp(), so the date can't drift a day across local/UTC."""
    return "Week of " + _dt.datetime.fromtimestamp(
        w["week_start_day"] * 86400, tz=_dt.timezone.utc).strftime("%b %-d")


def _day_label(day) -> str:
    """Weekday-qualified label for a UTC day number, e.g. 'Tue Feb 3' -- feeds the Month in review
    binge callout (#79), whose payload only carries a raw day int. Same fromtimestamp(day*86400,
    tz=utc) convention as _week_tip/month_of, so it can't drift a day across local/UTC."""
    return _dt.datetime.fromtimestamp(day * 86400, tz=_dt.timezone.utc).strftime("%a %b %-d")


def _months_ago(ts, now) -> int:
    """Whole months since a play timestamp `ts`, floor-divided by ~30-day months -- feeds the Library
    health rediscover list's "last played N months ago" (#80). rediscover_tracks() rows always have a
    real last_play (they're filtered to plays > 0), but guard None defensively anyway."""
    if ts is None:
        return 0
    return max(0, int((now - ts) // 86400 // 30))


def _week_axis(weeks) -> dict | None:
    if len(weeks) < 2:
        return None
    return {"first": _week_day_label(weeks[0]["week_start_day"]),
            "last": _week_day_label(weeks[-1]["week_start_day"])}


def _month_axis(months) -> dict | None:
    if len(months) < 2:
        return None
    return {"first": _month_label(months[0]["month"]), "last": _month_label(months[-1]["month"])}


def _rollup(store):
    return store.get_proposals("trend_rollups") or {}


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _listening_ctx(roll):
        weeks = roll.get("weeks", [])
        area = viz.area_spark([{"t": w["week_start_day"] * 86400.0, "n": w["plays"],
                                 "label": _week_tip(w)} for w in weeks])
        # genre-family ribbon: top families by total plays, each a share series across weeks
        totals = {}
        for w in weeks:
            for f, n in w["families"].items():
                totals[f] = totals.get(f, 0) + n
        fams = [f for f, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:5]]
        series = [{"key": f, "points": [{"t": w["week_start_day"] * 86400.0,
                                         "v": (w["families"].get(f, 0) / w["plays"]) if w["plays"] else 0.0}
                                        for w in weeks]} for f in fams]
        return {"area": area, "ribbon": viz.stacked_areas(series), "top_line": viz.stacked_top(series),
                "weeks": weeks, "families": fams, "x_axis": _week_axis(weeks)}

    @router.get("/trends")
    def trends_page(request: Request):
        roll = _rollup(store)
        return templates.TemplateResponse(request, "trends.html",
                                          {"roll": roll, "insights": roll.get("insights", []),
                                           **_listening_ctx(roll)})

    @router.get("/trends/discovery")
    def trends_discovery(request: Request):
        roll = _rollup(store)
        weeks = roll.get("weeks", [])
        floor = roll.get("first_play_floor_day")
        pts = [{"t": w["week_start_day"] * 86400.0,
                "v": (w["new_artist_plays"] / w["plays"]) if w["plays"] else 0.0,
                "censored": floor is not None and w["week_start_day"] <= floor + 7,
                "label": _week_tip(w)} for w in weeks]
        area = viz.line_area([p for p in pts], ymax=1.0)
        latest = next((p for p in reversed(pts) if not p["censored"]), None)
        return templates.TemplateResponse(request, "_partials/trend_discovery.html",
                                          {"area": area, "latest": latest, "have": len(pts) >= 2,
                                           "x_axis": _week_axis(weeks)})

    @router.get("/trends/diversity")
    def trends_diversity(request: Request):
        roll = _rollup(store)
        months = roll.get("months", [])
        pts = [{"t": _dt.datetime.strptime(m["month"] + "-01", "%Y-%m-%d").timestamp(),
                "v": m["diversity"],
                "label": _dt.datetime.strptime(m["month"] + "-01", "%Y-%m-%d").strftime("%b %Y")}
               for m in months]
        divs = [m["diversity"] for m in months]
        stats = {"min": min(divs), "max": max(divs), "current": divs[-1]} if divs else None
        return templates.TemplateResponse(request, "_partials/trend_diversity.html",
                                          {"area": viz.line_area(pts, ymax=1.0), "months": months,
                                           "stats": stats, "x_axis": _month_axis(months)})

    @router.get("/trends/review")
    def trends_review(request: Request):
        review = _rollup(store).get("review")
        binge_day_label = (_day_label(review["binge"]["day"])
                           if review and review.get("binge") else None)
        return templates.TemplateResponse(request, "_partials/trend_review.html",
                                          {"review": review,
                                           "month_name": _month_name(review["month"]) if review else None,
                                           "binge_day_label": binge_day_label})

    @router.get("/trends/health")
    def trends_health(request: Request):
        health = _rollup(store).get("health")
        if health:
            total = health["total_tracks"] or 1
            staleness = [{**b, "pct": round(b["n"] / total * 100, 2),
                          "color": _STALE_COLORS.get(b["bucket"], "var(--faint)")}
                         for b in health["staleness"]]
            # Dead-weight playlists dedupe by title (the same playlist title can legitimately appear
            # more than once, e.g. re-imported or synced from two sources) and cap the list so the card
            # can't turn into a 17-item bullet dump; the overflow count links out to /cleanup instead.
            seen = set()
            deduped = []
            for pl in health["dead_playlists"]:
                if pl["title"] in seen:
                    continue
                seen.add(pl["title"])
                deduped.append(pl)
            now = ctx.now()
            rediscover = [{**r, "months_ago": _months_ago(r.get("last_play"), now)}
                          for r in health.get("rediscover", [])]
            health = {**health, "staleness": staleness,
                      "dead_playlists": deduped[:DEAD_PLAYLISTS_CAP],
                      "dead_overflow": max(0, len(deduped) - DEAD_PLAYLISTS_CAP),
                      "rediscover": rediscover}
        return templates.TemplateResponse(request, "_partials/trend_health.html", {"health": health})

    return router
