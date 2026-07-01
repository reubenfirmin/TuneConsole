"""Taste-mode selection scoreboard (issue #60, Part C).

Aggregates the non-circular signal: how often each mode was offered (impressions), how often it was
picked (Save & play), and how much its picked playlists were then listened to (existing listen stats).
Pure read aggregation; writes nothing."""


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
