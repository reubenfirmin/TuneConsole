"""Monthly recap statistics over account-calendar day numbers."""
from datetime import datetime, timedelta, timezone

from yt_playlist.util import genre_map


def month_of(day):
    return datetime.fromtimestamp(day * 86400, tz=timezone.utc).strftime("%Y-%m")


def last_complete_month(now):
    first = datetime.fromtimestamp(now, tz=timezone.utc).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def diversity_index(families):
    total = sum(families.values())
    if total <= 0:
        return 0.0
    return sum((a_count / total) * (b_count / total) * genre_map.family_distance(a, b)
               for a, a_count in families.items() for b, b_count in families.items() if a != b)


def longest_streak(days):
    days = set(days)
    best = 0
    for day in days:
        if day - 1 not in days:
            run = 1
            while day + run in days:
                run += 1
            best = max(best, run)
    return best


def compute_months(day_counts, meta, artist_first, catalog_artists=frozenset()):
    """Calendar months with catalog-relative discovery and month-scoped streaks."""
    months = {}
    for day, key, cnt in day_counts:
        month = month_of(day)
        artist, genre = meta.get(key, ("", None))
        row = months.setdefault(month, {"plays": 0, "artists": set(), "new_artist_plays": 0,
                                        "new_artists": set(), "families": {}, "days": set()})
        row["plays"] += cnt
        row["days"].add(day)
        if artist:
            row["artists"].add(artist)
            if artist not in catalog_artists and month_of(artist_first.get(artist, day)) == month:
                row["new_artist_plays"] += cnt
                row["new_artists"].add(artist)
        if genre:
            family = genre_map.family(genre)
            row["families"][family] = row["families"].get(family, 0) + cnt
    return [{"month": month, "plays": row["plays"], "distinct_artists": len(row["artists"]),
             "new_artist_plays": row["new_artist_plays"], "new_artists": len(row["new_artists"]),
             "families": row["families"], "diversity": diversity_index(row["families"]),
             "listen_days": len(row["days"]), "longest_streak": longest_streak(row["days"])}
            for month, row in sorted(months.items())]


def month_day_counts(day_counts, month):
    y, mo = (int(x) for x in month.split("-"))
    start = int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp() // 86400)
    end = int(datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc).timestamp() // 86400)
    counts = {day: 0 for day in range(start, end)}
    for day, _key, cnt in day_counts:
        if day in counts:
            counts[day] += cnt
    return [{"day": day - start + 1, "n": count} for day, count in counts.items()]


def month_peak_day(day_counts, meta, month):
    totals, artists = {}, {}
    for day, key, cnt in day_counts:
        if month_of(day) != month:
            continue
        totals[day] = totals.get(day, 0) + cnt
        artist = meta.get(key, ("", None))[0]
        if artist:
            counts = artists.setdefault(day, {})
            counts[artist] = counts.get(artist, 0) + cnt
    if not totals:
        return None
    day = max(totals, key=lambda d: (totals[d], d))
    top = max(artists.get(day, {}).items(), key=lambda kv: (kv[1], kv[0]), default=None)
    return {"day": day, "plays": totals[day], "artist": top[0] if top else None,
            "pct": top[1] / totals[day] if top else 0.0}
