"""Charts "ticker" assembly: turn a corpus distribution + windowed listen distributions into
ranked rows on a magnitude axis.

The chart reads as a ranked bar chart (like the Songs/Artists tabs): bar length = the category's
recent listen share, so rows visibly shrink down the ranking. Layered on top: a per-row dashed
tick at the category's CORPUS share (bar past the tick = over-indexed, "punching above its
weight"), a faint marker for where it sat a year ago, and a thin whisker for its min/max share
across the periods. Pure functions — DB queries live in ChartsRepo, SVG/CSS in the template.
"""
def _shares(dist: dict) -> dict:
    """Normalize a {category: count} distribution to {category: fraction-of-total}."""
    total = sum(dist.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in dist.items()}


def ticker_rows(corpus: dict, windows: dict) -> dict:
    """Build ranked ticker rows from a corpus distribution and per-period listen distributions.

    corpus:  {category: song_count} (library composition; the per-row baseline tick).
    windows: ordered {period_label: {category: play_count}}, NEWEST first. A period with no plays
             at all (empty dict) is treated as NULL — skipped, not counted as 0% — so a young
             library's empty older periods don't fabricate a flat "earlier" reading.

    Returns {"rows": [row, ...], "axis_max": float}. Rows sort by recent share (close) desc.
    Per row: cat, corpus_share, close (newest period's share), open (oldest *populated* period's
    share, or None when there's no earlier data), high/low (range across populated periods),
    ratio (close / corpus_share, or None), trend ("up"/"down"/"flat"; "flat" when open is None).
    """
    corpus_share = _shares(corpus)
    order = list(windows.keys())
    populated = [w for w in order if sum(windows[w].values()) > 0]
    win_share = {w: _shares(windows[w]) for w in order}
    newest = order[0] if order else None
    earlier = [w for w in order[1:] if w in populated]   # populated periods older than the newest

    cats = set(corpus_share)
    for s in win_share.values():
        cats |= set(s)

    rows = []
    for cat in cats:
        base = corpus_share.get(cat, 0.0)
        close = win_share.get(newest, {}).get(cat, 0.0) if newest else 0.0
        open_ = win_share[earlier[-1]].get(cat, 0.0) if earlier else None
        pop_shares = [win_share[w].get(cat, 0.0) for w in populated]
        if open_ is None:
            trend = "flat"
        elif close > open_:
            trend = "up"
        elif close < open_:
            trend = "down"
        else:
            trend = "flat"
        rows.append({
            "cat": cat,
            "corpus_share": base, "open": open_, "close": close,
            "high": max(pop_shares) if pop_shares else 0.0,
            "low": min(pop_shares) if pop_shares else 0.0,
            "ratio": (close / base) if base > 0 else None,
            "trend": trend,
        })

    rows.sort(key=lambda r: (-r["close"], -r["corpus_share"], r["cat"]))
    axis_max = max((max(r["close"], r["corpus_share"]) for r in rows), default=0.0)
    return {"rows": rows, "axis_max": axis_max or 1.0}


def candle_geometry(row: dict, axis_max: float) -> dict:
    """Map one row's absolute shares onto the 0..axis_max axis as percentages (0-100).

    Returns the ranked bar length (bar_pct = recent share), the corpus baseline tick, the earlier
    marker, the whisker ends, whether the bar is over-indexed, and the trend class. Guards
    axis_max <= 0 -> all zero; clamps to [0, 100].
    """
    def pct(v):
        if axis_max <= 0:
            return 0.0
        return max(0.0, min(100.0, v / axis_max * 100.0))

    return {
        "bar_pct": pct(row["close"]),
        "corpus_pct": pct(row["corpus_share"]),
        "open_pct": pct(row["open"]) if row["open"] is not None else None,
        "whisker_lo_pct": pct(row["low"]),
        "whisker_hi_pct": pct(row["high"]),
        "over": row["close"] >= row["corpus_share"] and row["corpus_share"] > 0,
        "trend": row["trend"],
    }
