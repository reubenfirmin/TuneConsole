"""Taste-mode selection scoreboard (issue #60, Part C).

Aggregates the non-circular signal: how often each mode was offered (impressions), how often it was
picked (Save & play), and how much its picked playlists were then listened to (existing listen stats).
Pick and impression counts drive theme selection across the row via Thompson sampling; mode_scoreboard
also provides display aggregation. Reads only; writes nothing."""

import numpy as np


def choice_posterior(picks, offers):
    """Parameters of the selection distribution used by the Home theme sampler."""
    return 1 + picks, 1 + max(0, offers - picks)


def _choice_contrast(rows, strongest):
    """One supported contrast, not predicted menu shares or absolute increases.

    Compare the best-supported choice with the most-offered group whose middle-80% interval
    is entirely below it. Untested groups remain exploratory. Identical labels are separate
    model groups and must not be merged or presented as opposing preferences.
    """
    if not strongest:
        return None
    leader = strongest[0]
    for row in sorted(rows, key=lambda r: (-r["offered"], r["mode_id"])):
        if (row["has_evidence"] and row["high"] < leader["low"]
                and row["label"].casefold() != leader["label"].casefold()):
            return {"favored": [leader], "less_supported": [row]}
    return None


def choice_evidence(store):
    """Expose the sampler's evidence without interpreting saves as confirmed listening.

    Picks are successful playlist creations, not unique clicks per offer. Preserve both raw
    counts, including repeat selections, and use the sampler's bounded posterior rather than
    presenting their ratio as a click-through rate. Intervals use a fixed simulation so the
    read-only chart stays stable between visits and needs no statistics dependency.
    """
    offered = store.modes.impression_counts()
    picked = {}
    for _playlist_id, mid in store.modes.pick_rows():
        picked[mid] = picked.get(mid, 0) + 1
    rows = []
    for color, mode in enumerate(store.modes.list_modes(active_only=True)):
        mid = mode["mode_id"]
        offers, picks = offered.get(mid, 0), picked.get(mid, 0)
        # The sampler ignores orphan picks until an impression exists for that mode.
        alpha, beta = choice_posterior(picks if mid in offered else 0, offers)
        rng = np.random.default_rng(mid)
        low, high = np.quantile(rng.beta(alpha, beta, 16384), [0.1, 0.9])
        rows.append({"mode_id": mid, "label": mode["label"], "color_idx": color,
                     "offered": offers, "picked": picks, "has_evidence": mid in offered,
                     "alpha": alpha, "beta": beta,
                     "estimate": alpha / (alpha + beta), "low": float(low), "high": float(high)})
    strongest = sorted((r for r in rows if r["picked"] and r["offered"]),
                       key=lambda r: (-r["low"], r["mode_id"]))[:3]
    return {"rows": rows, "strongest": strongest, "contrast": _choice_contrast(rows, strongest),
            "offered": sum(r["offered"] for r in rows),
            "picked": sum(r["picked"] for r in rows)}


def mode_bandit_stats(store, *, before=None) -> dict:
    """{mode_id: (picks, impressions)}, pooled across framings. `before` freezes the evidence at
    a menu's first display so its own impressions/picks only influence the next rotation."""
    offered = store.modes.impression_counts(before=before)
    picked = {}
    for _playlist_id, mode_id in store.modes.pick_rows(before=before):
        picked[mode_id] = picked.get(mode_id, 0) + 1
    return {mid: (picked.get(mid, 0), n) for mid, n in offered.items()}


def mode_scoreboard(store, since=None) -> list[dict]:
    """Per active mode: {mode_id, label, offered, picked, plays, last_play}, ordered by offered desc."""
    modes = store.modes.list_modes(active_only=True)
    offered = store.modes.impression_counts(since)
    picks = store.modes.pick_rows(since)
    stats = store.charts.get_playlist_listen_stats()        # {playlist_id: (last_listen_ts, count)}
    picked_count, plays, last_play = {}, {}, {}
    for playlist_id, mode_id in picks:
        picked_count[mode_id] = picked_count.get(mode_id, 0) + 1
        last, cnt = stats.get(playlist_id, (None, 0))
        plays[mode_id] = plays.get(mode_id, 0) + (cnt or 0)
        if last is not None and (last_play.get(mode_id) is None or last > last_play[mode_id]):
            last_play[mode_id] = last
    board = []
    for m in modes:
        mid = m["mode_id"]
        board.append({"mode_id": mid, "label": m["label"],
                      "offered": offered.get(mid, 0), "picked": picked_count.get(mid, 0),
                      "plays": plays.get(mid, 0), "last_play": last_play.get(mid)})
    board.sort(key=lambda b: (-b["offered"], b["mode_id"]))
    return board


def ranker_scoreboard(store, since=None) -> list[dict]:
    """#57 A/B verdict for the in-mode ranker, judged by the #60 selection log (NOT temporal_recall).
    Per ranker ('ppr' vs 'cosine'): how often it was offered, how often its card was picked (Save &
    play), and how much its picked playlists were then listened to. NULL rankers (pre-#57 rows) count
    as 'cosine', the ranker they were served under. Reads only."""
    offered = store.modes.ranker_impression_counts(since)      # {ranker: impressions}
    picks = store.modes.ranker_pick_rows(since)                # [(playlist_id, ranker)]
    stats = store.charts.get_playlist_listen_stats()           # {playlist_id: (last_listen_ts, count)}
    picked, plays = {}, {}
    for playlist_id, ranker in picks:
        picked[ranker] = picked.get(ranker, 0) + 1
        _last, cnt = stats.get(playlist_id, (None, 0))
        plays[ranker] = plays.get(ranker, 0) + (cnt or 0)
    rankers = set(offered) | set(picked)
    return [{"ranker": rk, "offered": offered.get(rk, 0), "picked": picked.get(rk, 0),
             "plays": plays.get(rk, 0)} for rk in sorted(rankers)]
