"""Pure UTC calendar, listening-day and rollup statistics for detached play rows."""
from datetime import datetime, timedelta, timezone

from yt_playlist.util import genre_map


def week_start(day) -> int:
    """Epoch-anchored week over UTC day numbers, rather than ISO calendar weeks."""
    return (day // 7) * 7


def month_of(day) -> str:
    return datetime.fromtimestamp(day * 86400, tz=timezone.utc).strftime("%Y-%m")


def last_complete_month(now) -> str:
    first = datetime.fromtimestamp(now, tz=timezone.utc).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def month_succ(month):
    year, number = (int(x) for x in month.split("-"))
    return f"{year + (number == 12):04d}-{(number % 12) + 1:02d}"


def latest_full_week_start(week_keys, now_day):
    full = [w for w in sorted(week_keys) if w + 7 <= now_day]
    return full[-1] if full else None


def diversity_index(families) -> float:
    """Expected genre-family distance between two random plays of a period."""
    total = sum(families.values())
    if total <= 0:
        return 0.0
    fams = list(families)
    distance = 0.0
    for a in fams:
        share = families[a] / total
        for b in fams:
            if a != b:
                distance += share * (families[b] / total) * genre_map.family_distance(a, b)
    return distance


def longest_streak(days) -> int:
    """Longest run within the supplied UTC days, including a month-scoped subset."""
    days = set(days)
    best = 0
    for day in days:
        if day - 1 not in days:
            run = 1
            while day + run in days:
                run += 1
            best = max(best, run)
    return best


def current_streak(days) -> int:
    """Run ending at the latest listen day, without splitting at month boundaries."""
    days = set(days)
    if not days:
        return 0
    latest = max(days)
    run = 0
    while latest - run in days:
        run += 1
    return run


def median(values):
    values = sorted(values)
    n = len(values)
    if n == 0:
        return 0.0
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0


def compute_weeks(day_counts, meta, artist_first, track_first,
                  catalog_artists=frozenset(), catalog_keys=frozenset()) -> list:
    """Discovery is catalog-relative: first observed does not mean newly discovered.

    Plays of owned artists/tracks never count as discoveries simply because our observation
    window began recently. Families use each track's representative genre.
    """
    weeks = {}
    for day, key, cnt in day_counts:
        w = week_start(day)
        artist, genre = meta.get(key, ("", None))
        wk = weeks.setdefault(w, {"plays": 0, "artists": set(), "new_artist_plays": 0,
                                  "new_artists": set(), "new_track_plays": 0, "families": {}})
        wk["plays"] += cnt
        if artist:
            wk["artists"].add(artist)
            if artist not in catalog_artists and week_start(artist_first.get(artist, day)) == w:
                wk["new_artist_plays"] += cnt
                wk["new_artists"].add(artist)
        if key not in catalog_keys and week_start(track_first.get(key, day)) == w:
            wk["new_track_plays"] += cnt
        if genre:
            fam = genre_map.family(genre)
            wk["families"][fam] = wk["families"].get(fam, 0) + cnt
    out = []
    for w in sorted(weeks):
        wk = weeks[w]
        out.append({"week_start_day": w, "plays": wk["plays"],
                    "distinct_artists": len(wk["artists"]),
                    "new_artist_plays": wk["new_artist_plays"],
                    "new_artists": len(wk["new_artists"]),
                    "new_track_plays": wk["new_track_plays"],
                    "families": wk["families"], "diversity": diversity_index(wk["families"])})
    return out


def compute_months(day_counts, meta, artist_first, catalog_artists=frozenset()) -> list:
    """Calendar months with distinct listening days and a month-scoped longest streak."""
    months = {}
    for day, key, cnt in day_counts:
        m = month_of(day)
        artist, genre = meta.get(key, ("", None))
        mo = months.setdefault(m, {"plays": 0, "artists": set(), "new_artist_plays": 0,
                                   "new_artists": set(), "families": {}, "days": set()})
        mo["plays"] += cnt
        mo["days"].add(day)
        if artist:
            mo["artists"].add(artist)
            if artist not in catalog_artists and month_of(artist_first.get(artist, day)) == m:
                mo["new_artist_plays"] += cnt
                mo["new_artists"].add(artist)
        if genre:
            fam = genre_map.family(genre)
            mo["families"][fam] = mo["families"].get(fam, 0) + cnt
    out = []
    for m in sorted(months):
        mo = months[m]
        out.append({"month": m, "plays": mo["plays"], "distinct_artists": len(mo["artists"]),
                    "new_artist_plays": mo["new_artist_plays"], "new_artists": len(mo["new_artists"]),
                    "families": mo["families"], "diversity": diversity_index(mo["families"]),
                    "listen_days": len(mo["days"]), "longest_streak": longest_streak(mo["days"])})
    return out


def month_day_counts(day_counts, month) -> list:
    """Zero-filled calendar heat strip: [{day: 1-indexed day-of-month, n: plays}]."""
    y, mo = (int(x) for x in month.split("-"))
    start_day = int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp() // 86400)
    end_day = int(datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc).timestamp() // 86400)
    counts = {d: 0 for d in range(start_day, end_day)}
    for day, _key, cnt in day_counts:
        if day in counts:
            counts[day] += cnt
    return [{"day": d - start_day + 1, "n": counts[d]} for d in sorted(counts)]


def month_peak_day(day_counts, meta, month):
    """Biggest listening day and its top artist's concentration, or None."""
    dt, da = {}, {}
    for day, key, cnt in day_counts:
        if month_of(day) != month:
            continue
        dt[day] = dt.get(day, 0) + cnt
        artist = meta.get(key, ("", None))[0]
        if artist:
            da.setdefault(day, {})[artist] = da.setdefault(day, {}).get(artist, 0) + cnt
    if not dt:
        return None
    day = max(dt, key=lambda d: (dt[d], d))
    top = max(da.get(day, {}).items(), key=lambda kv: (kv[1], kv[0]), default=None)
    return {"day": day, "plays": dt[day], "artist": top[0] if top else None,
            "pct": (top[1] / dt[day]) if top else 0.0}


def compute_overlay_series(day_counts, meta, weeks, top_artists=8, top_genres=6):
    """Per-entity weekly series aligned with the rollup weeks, ranked by total plays."""
    idx = {w["week_start_day"]: i for i, w in enumerate(weeks)}
    n = len(weeks)
    a_week, a_total = {}, {}
    for day, key, cnt in day_counts:
        i = idx.get(week_start(day))
        if i is None:
            continue
        artist, _genre = meta.get(key, ("", None))
        if artist:
            a_total[artist] = a_total.get(artist, 0) + cnt
            a_week.setdefault(artist, [0] * n)[i] += cnt
    g_week, g_total = {}, {}
    for i, w in enumerate(weeks):
        for fam, cnt in w["families"].items():
            g_week.setdefault(fam, [0] * n)[i] += cnt
            g_total[fam] = g_total.get(fam, 0) + cnt
    top_a = sorted(a_total, key=lambda a: (-a_total[a], a))[:top_artists]
    top_g = sorted(g_total, key=lambda f: (-g_total[f], f))[:top_genres]
    return {"artists": [{"key": "artist:" + a, "label": a, "total": a_total[a], "weekly": a_week[a]}
                        for a in top_a],
            "genres": [{"key": "genre:" + f, "label": f, "total": g_total[f], "weekly": g_week[f]}
                       for f in top_g]}


def fold_plays(day_counts, meta):
    """One pass builds the per-dimension counts consumed by the insight detectors."""
    aw, am, ad, td, tw, dt, da = {}, {}, {}, {}, {}, {}, {}
    for day, key, cnt in day_counts:
        w, m = week_start(day), month_of(day)
        dt[day] = dt.get(day, 0) + cnt
        td.setdefault(key, {})[day] = td.setdefault(key, {}).get(day, 0) + cnt
        tw.setdefault(w, {})[key] = tw.setdefault(w, {}).get(key, 0) + cnt
        artist = meta.get(key, ("", None))[0]
        if not artist:
            continue
        aw.setdefault(artist, {})[w] = aw.setdefault(artist, {}).get(w, 0) + cnt
        am.setdefault(artist, {})[m] = am.setdefault(artist, {}).get(m, 0) + cnt
        ad.setdefault(artist, {})[day] = ad.setdefault(artist, {}).get(day, 0) + cnt
        da.setdefault(day, {})[artist] = da.setdefault(day, {}).get(artist, 0) + cnt
    return {"artist_week": aw, "artist_month": am, "artist_day": ad, "track_day": td,
            "track_week": tw, "day_total": dt, "day_artist": da}
