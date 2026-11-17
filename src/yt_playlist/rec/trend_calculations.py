"""Monthly review calculations over supplied calendar, catalog and listening facts."""
from datetime import datetime, timezone

from yt_playlist.rec import trend_statistics as stats


def month_review(months, now, *, this, prev, labels, artist_first, catalog, track_plays,
                 cards, artwork, day_counts=(), meta=None):
    """Previous complete calendar month; exact counts use each account's retained dates."""
    month = stats.last_complete_month(now)
    cur = next((m for m in months if m["month"] == month), None)
    if cur is None:
        return None
    y, mo = (int(x) for x in month.split("-"))
    label = lambda aid: labels.get(aid,aid)
    deltas = {a: this.get(a, 0) - prev.get(a, 0) for a in set(this) | set(prev)}
    riser = max(deltas.items(), key=lambda kv: kv[1], default=None)
    faller = min(deltas.items(), key=lambda kv: kv[1], default=None)
    new_this = {a: this.get(a, 0) for a, day in artist_first.items()
                if stats.month_of(day) == month and a not in catalog}
    top_new = max(new_this.items(), key=lambda kv: kv[1], default=None)
    top_artists = [{"artist": label(a), "artist_id":a, "plays": n, "art": artwork.get(a)}
                   for a, n in sorted(this.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    tk = max(track_plays.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    if tk:
        card = cards.get(tk[0], {})
        top_track = {"track": tk[0], "title": card.get("title"), "artist": card.get("artist", ""),
                     "plays": tk[1], "art": card.get("thumbnail")}
    else:
        top_track = None
    binge = stats.month_peak_day(day_counts, meta or {}, month)
    if binge and binge['artist']:
        binge['artist'] = label(binge['artist'])
    return {"month": month, "plays": cur["plays"], "listen_days": cur["listen_days"],
            "longest_streak": cur["longest_streak"],
            "top_new_artist": ({"artist": label(top_new[0]), "plays": top_new[1]}
                               if top_new and top_new[1] > 0 else None),
            "riser": ({"artist": label(riser[0]), "delta": riser[1]} if riser and riser[1] > 0 else None),
            "faller": ({"artist": label(faller[0]), "delta": faller[1]} if faller and faller[1] < 0 else None),
            "play_days": stats.month_day_counts(day_counts, month),
            "first_weekday": (datetime(y, mo, 1, tzinfo=timezone.utc).weekday() + 1) % 7,
            "top_artists": top_artists, "top_track": top_track,
            "binge": binge}


