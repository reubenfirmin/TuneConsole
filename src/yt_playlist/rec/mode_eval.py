"""Taste-mode selection scoreboard (issue #60, Part C).

Aggregates the non-circular signal: how often each mode was offered (impressions), how often it was
picked (Save & play), and how much its picked playlists were then listened to (existing listen stats).
Pick and impression counts drive dominant-mode selection via Thompson sampling (#87); mode_scoreboard
also provides display aggregation. Reads only; writes nothing."""


def mode_bandit_stats(store) -> dict:
    """#87 {mode_id: (picks, impressions)} over all time: the Thompson sampler's evidence. Reads
    the SAME rows as mode_scoreboard; this is the moment those counts stop being display-only."""
    offered = store.modes.impression_counts()
    picked = {}
    for _playlist_id, mode_id in store.modes.pick_rows():
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
