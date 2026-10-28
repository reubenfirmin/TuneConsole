"""The live monthly recap, assembled on the rollup's private snapshot reader."""
from datetime import datetime, timezone

from yt_playlist.rec import recap_details, recap_story, trend_statistics as stats


def month_review(months, store, now, day_counts=(), meta=None):
    """Previous complete calendar month; exact counts use each account's retained dates."""
    month = stats.last_complete_month(now)
    cur = next((m for m in months if m["month"] == month), None)
    if cur is None:
        return None
    y, mo = (int(x) for x in month.split("-"))
    start = datetime(y, mo, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    pstart = datetime(y - (mo == 1), ((mo - 2) % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    ledger = store.trends.has_play_ledger()
    if ledger:
        this = store.trends.ledger_artist_plays(start, end, calendar=True)
        prev = store.trends.ledger_artist_plays(pstart, start, calendar=True)
    else:
        this = store.charts.listen_distribution("artist", since=start, until=end)
        prev = store.charts.listen_distribution("artist", since=pstart, until=start)
    deltas = {a: this.get(a, 0) - prev.get(a, 0) for a in set(this) | set(prev)}
    riser = max(deltas.items(), key=lambda kv: kv[1], default=None)
    faller = min(deltas.items(), key=lambda kv: kv[1], default=None)
    afp = store.trends.first_play_map("artist")
    catalog = store.trends.catalog_artists()
    new_this = {a: this.get(a, 0) for a, day in afp.items()
                if stats.month_of(day) == month and a not in catalog}
    top_new = max(new_this.items(), key=lambda kv: kv[1], default=None)
    top_artists = [{"artist": a, "plays": n, "art": store.charts.artist_thumbnail(a)}
                   for a, n in sorted(this.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    mtp = (store.trends.ledger_track_plays(start, end, calendar=True) if ledger
           else store.trends.month_track_plays(start, end))
    tk = max(mtp.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    if tk:
        card = store.trends.track_cards([tk[0]]).get(tk[0], {})
        top_track = {"track": tk[0], "title": card.get("title"), "artist": card.get("artist", ""),
                     "plays": tk[1], "art": card.get("thumbnail")}
    else:
        top_track = None
    return {"month": month, "plays": cur["plays"], "listen_days": cur["listen_days"],
            "longest_streak": cur["longest_streak"],
            "top_new_artist": ({"artist": top_new[0], "plays": top_new[1]}
                               if top_new and top_new[1] > 0 else None),
            "riser": ({"artist": riser[0], "delta": riser[1]} if riser and riser[1] > 0 else None),
            "faller": ({"artist": faller[0], "delta": faller[1]} if faller and faller[1] < 0 else None),
            "play_days": stats.month_day_counts(day_counts, month),
            "first_weekday": (datetime(y, mo, 1, tzinfo=timezone.utc).weekday() + 1) % 7,
            "top_artists": top_artists, "top_track": top_track,
            "binge": stats.month_peak_day(day_counts, meta or {}, month)}


def assemble(store, now, day_counts, meta, months):
    """Only the recap is published; its routes and Home consume the story field."""
    review = month_review(months, store, now, day_counts, meta)
    month_name = (datetime.strptime(review["month"] + "-01", "%Y-%m-%d").strftime("%B")
                  if review else None)
    details = recap_details.build(store, review["month"]) if review else []
    story = recap_story.build_story(store, months, review, month_name, detail_beats=details)
    return {"built_at": now, "story": story}
