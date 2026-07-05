"""#76-#80 Trends rollups: precompute the weekly/monthly listening rollup, the last-month recap, the
library-health snapshot, and (at most one) Home spotlight candidate, materialized via put_proposals.
Runs in the rec worker only. All play counts use the day model (history_snapshots + history_items);
play_events is used ONLY in the first-play index (a MIN), never as an added play count.

Thresholds below are EDITORIAL (when to interrupt the user), not model knobs, so they are constants
here, not rec_params."""
from datetime import datetime, timezone
from urllib.parse import quote

from yt_playlist.util import genre_map

SPOTLIGHT_MIN_INTERVAL_S = 5 * 86400    # at least 5 days between any two spotlights, however interesting
SPOTLIGHT_SNOOZE_S       = 30 * 86400   # a dismissed spotlight signature stays snoozed 30 days
DISCOVERY_SPIKE_RATIO    = 2.0          # latest week's new-artist plays vs the trailing-8-week median
DISCOVERY_SPIKE_MIN_ABS  = 5            # ...and an absolute floor so a 1->3 blip never trips it
DIVERSITY_WINDOW_DAYS    = 90           # "diversity at a 90-day high / low"
DIVERSITY_MIN_MONTHS     = 3            # need this many months in the window before "extreme" is meaningful
STREAK_THRESHOLDS        = (7, 30, 100) # daily-listening streak milestones (each fires once)
TOP_FAMILIES             = 5            # "a genre family newly entering the top 5"

FLOOR_CENSOR_DAYS       = 7    # left-censoring warm-up: a first_day <= floor_day+7 is "we have no prior",
                               # not "genuinely new" (matches the existing discovery `floor_day + 7`).
EMERGENCE_MAX_AGE_DAYS  = 42   # EMERGENCE: artist first heard within 6 weeks
EMERGENCE_MIN_LATEST    = 8    # ...and the latest FULL week has >= 8 plays (thin-data floor)
EMERGENCE_GROWTH        = 3.0  # ...and latest full week >= 3x its own earliest non-zero week
PTB_MONTHLY_MIN         = 8    # PUT TO BED: "sustained" = >= 8 plays in a calendar month
PTB_MONTHS              = 2    # ...for >= 2 consecutive months
PTB_SILENCE_DAYS        = 42   # ...then zero plays for >= 6 weeks
REPETITION_MIN_RETURNS  = 3    # REPETITION: >= 3 strictly-shrinking return intervals (>= 4 real plays)
REVIVAL_DORMANT_DAYS    = 180  # REVIVAL: dormant >= 6 months after real prior presence
REVIVAL_RECENT_DAYS     = 7    # ...with plays in the last week
REVIVAL_RECENT_MIN      = 3    # ...and >= 3 plays in that last week
ONE_SONG_MIN_PLAYS      = 10   # ONE-SONG ARTIST: >= 10 plays, all on a single track, 0 on any other
BINGE_RATIO             = 3.0  # BINGE DAY: a day >= 3x the trailing-median daily volume
BINGE_TRAILING_DAYS     = 28   # ...median over the prior 28 listen-days
BINGE_MIN_PRIOR_DAYS    = 7    # ...need >= 7 prior listen-days before "3x median" means anything
BINGE_CONCENTRATION     = 0.50 # ...and >= 50% of the day's plays are one artist
SOTW_MIN_PLAYS          = 5    # SONG OF THE WEEK: most-repeated track of the latest FULL week, >= 5 plays
INSIGHTS_MAX            = 5    # render at most 5 insight cards; spotlight uses only rank 1
RECENCY_HORIZON_DAYS    = 42   # recency weight decays linearly to 0 over 6 weeks
MAGNITUDE_CAP           = 4.0  # magnitude is clipped here before normalizing to [0,1]
REDISCOVER_QUIET_DAYS   = 90   # REDISCOVER: an owned track must be quiet this long to be a candidate
INSIGHT_RARITY = {             # per-KIND surprise prior (editorial; NOT estimated from population freq,
    "revival": 1.00,           # which we cannot observe). Rarer/more-surprising phenomena weigh more;
    "put_to_bed": 0.90,        # song_of_week fires ~weekly so it is the least surprising.
    "emergence": 0.80,
    "repetition": 0.70,
    "one_song": 0.60,
    "binge": 0.50,
    "song_of_week": 0.35,
}


def week_start(day) -> int:
    """Epoch-anchored week bucket: (day // 7) * 7 over UTC days (day = int(ts // 86400))."""
    return (day // 7) * 7


def month_of(day) -> str:
    """UTC 'YYYY-MM' for a UTC day number."""
    return datetime.fromtimestamp(day * 86400, tz=timezone.utc).strftime("%Y-%m")


def diversity_index(families) -> float:
    """Expected genre-family distance between two random plays of a period: sum_i sum_j s_i s_j
    family_distance(i, j), with s_f = family f's share of plays. In [0, 1]; 0 for a single family."""
    total = sum(families.values())
    if total <= 0:
        return 0.0
    fams = list(families)
    d = 0.0
    for a in fams:
        sa = families[a] / total
        for b in fams:
            if a == b:
                continue
            d += sa * (families[b] / total) * genre_map.family_distance(a, b)
    return d


def longest_streak(days) -> int:
    """Longest run of consecutive UTC days present in `days` (a set/iterable of ints)."""
    s = set(days)
    best = 0
    for d in s:
        if d - 1 not in s:                       # d starts a run
            run = 1
            while d + run in s:
                run += 1
            best = max(best, run)
    return best


def current_streak(days) -> int:
    """Length of the run of consecutive UTC days ending at max(days): the streak still ACTIVE as of the
    latest listen day, unbounded by any month boundary. Empty input -> 0.

    Distinct from longest_streak: longest_streak is used per-month by compute_months (it reports the
    longest run found within that one calendar month's days, which is the right stat for the month-in-
    review recap). current_streak instead looks across the whole day set and only cares about the run
    that reaches the most recent day, so a streak spanning e.g. Jan 29-31 + Feb 1-4 counts as 7, not two
    separate month-scoped runs of 3 and 4. That's what a "you're on an N-day streak" spotlight needs."""
    s = set(days)
    if not s:
        return 0
    latest = max(s)
    run = 0
    while latest - run in s:
        run += 1
    return run


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def discovery_spike(counts) -> bool:
    """True when the latest week's new-artist plays spike over the trailing 8-week median AND clear the
    absolute floor. counts = new_artist_plays per week, ascending; uses the up-to-8 weeks before the
    last as the baseline."""
    if len(counts) < 2:
        return False
    latest = counts[-1]
    base = _median(counts[-9:-1])
    return latest >= DISCOVERY_SPIKE_RATIO * base and latest >= DISCOVERY_SPIKE_MIN_ABS


def compute_weeks(day_counts, meta, artist_first, track_first) -> list:
    """weeks array. A play in week W counts as a new-artist play iff its artist's first_day is in W
    (same for new-track). Families come from each key's representative genre via genre_map.family."""
    weeks = {}
    for day, key, cnt in day_counts:
        w = week_start(day)
        artist, genre = meta.get(key, ("", None))
        wk = weeks.setdefault(w, {"plays": 0, "artists": set(), "new_artist_plays": 0,
                                  "new_track_plays": 0, "families": {}})
        wk["plays"] += cnt
        if artist:
            wk["artists"].add(artist)
            if week_start(artist_first.get(artist, day)) == w:
                wk["new_artist_plays"] += cnt
        if week_start(track_first.get(key, day)) == w:
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
                    "new_track_plays": wk["new_track_plays"],
                    "families": wk["families"], "diversity": diversity_index(wk["families"])})
    return out


def compute_months(day_counts, meta, artist_first) -> list:
    """months array (same fields as weeks minus new_track, plus listen_days + longest_streak)."""
    months = {}
    for day, key, cnt in day_counts:
        m = month_of(day)
        artist, genre = meta.get(key, ("", None))
        mo = months.setdefault(m, {"plays": 0, "artists": set(), "new_artist_plays": 0,
                                   "families": {}, "days": set()})
        mo["plays"] += cnt
        mo["days"].add(day)
        if artist:
            mo["artists"].add(artist)
            if month_of(artist_first.get(artist, day)) == m:
                mo["new_artist_plays"] += cnt
        if genre:
            fam = genre_map.family(genre)
            mo["families"][fam] = mo["families"].get(fam, 0) + cnt
    out = []
    for m in sorted(months):
        mo = months[m]
        out.append({"month": m, "plays": mo["plays"], "distinct_artists": len(mo["artists"]),
                    "new_artist_plays": mo["new_artist_plays"], "families": mo["families"],
                    "diversity": diversity_index(mo["families"]),
                    "listen_days": len(mo["days"]), "longest_streak": longest_streak(mo["days"])})
    return out


def _build_first_play_index(store):
    """Incremental fold of the first-play index. Snapshots normally only accrue forward, so an existing
    first_day can only be lowered by a Takeout backfill; that case clears + full-rescans."""
    ti = store.get_setting("takeout_imported_at")
    if ti is not None and ti != store.get_setting("trend_first_play_takeout_seen"):
        store.trends.clear_first_play()
        store.set_setting("trend_first_play_watermark", "0")
        store.set_setting("trend_first_play_takeout_seen", str(ti))
    wm = int(store.get_setting("trend_first_play_watermark") or 0)
    hist = store.trends.history_track_first(wm)
    plays = store.trends.play_event_track_first()
    rows = []
    for key in set(hist) | set(plays):
        cands = []
        if key in hist:
            cands.append((hist[key][1], hist[key][0], "history"))
        if key in plays:
            cands.append((plays[key][1], plays[key][0], "play_event"))
        ts, day, src = min(cands)
        rows.append(("track", key, day, ts, src))
    if rows:
        store.trends.upsert_first_play_min(rows)
    store.trends.rebuild_artist_first_play()
    store.set_setting("trend_first_play_watermark", str(store.trends.max_snapshot_id()))


def month_day_counts(day_counts, month) -> list:
    """Per-day-of-month play counts for `month` (a 'YYYY-MM' string), one entry per calendar day of
    that month in ascending order, zero-filled for days with no listens. Feeds the Month in review
    "heat strip" (#79): [{day: 1-indexed day-of-month, n: plays that day}]. day_counts is the same
    [(day, identity_key, count)] rows build() already reads from play_day_counts()."""
    y, mo = (int(x) for x in month.split("-"))
    start_day = int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp() // 86400)
    end_day = int(datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc).timestamp() // 86400)
    counts = {d: 0 for d in range(start_day, end_day)}
    for day, _key, cnt in day_counts:
        if day in counts:
            counts[day] += cnt
    return [{"day": d - start_day + 1, "n": counts[d]} for d in sorted(counts)]


def month_peak_day(day_counts, meta, month):
    """The single biggest listening day of `month`, with its top artist's concentration, or None."""
    dt, da = {}, {}
    for day, key, cnt in day_counts:
        if month_of(day) != month:
            continue
        dt[day] = dt.get(day, 0) + cnt
        a = meta.get(key, ("", None))[0]
        if a:
            da.setdefault(day, {})[a] = da.setdefault(day, {}).get(a, 0) + cnt
    if not dt:
        return None
    day = max(dt, key=lambda d: (dt[d], d))
    top = max(da.get(day, {}).items(), key=lambda kv: (kv[1], kv[0]), default=None)
    return {"day": day, "plays": dt[day], "artist": top[0] if top else None,
            "pct": (top[1] / dt[day]) if top else 0.0}


def month_review(months, store, now, day_counts=(), meta=None):
    """Recap of the last fully-elapsed calendar month, or None if none exists. months is ascending.
    day_counts (optional, defaults to ()) is the raw play_day_counts() rows, used to build the per-day
    heat-strip data (`play_days`) and the binge callout (`binge`); meta (optional, defaults to {}) is
    track_meta()'s {key: (artist, genre)}, used only by month_peak_day. Callers that don't care about
    either may omit them. `now` is the same wall-clock-or-simulated epoch seconds build() receives and
    threads everywhere else, so "past" months are judged against the caller's now, not real wall-clock
    time.time() (which would make every synthetic-now test/backfill diverge from what it's simulating)."""
    meta = meta or {}
    now_month = month_of(int(now // 86400))
    past = [m for m in months if m["month"] < now_month]
    if not past:
        return None
    cur = past[-1]
    # riser / faller vs the prior month, reusing the ticker's own artist play query
    y, mo = (int(x) for x in cur["month"].split("-"))
    start = datetime(y, mo, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    pstart = datetime(y - (mo == 1), ((mo - 2) % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    this = store.charts.listen_distribution("artist", since=start, until=end)
    prev = store.charts.listen_distribution("artist", since=pstart, until=start)
    deltas = {a: this.get(a, 0) - prev.get(a, 0) for a in set(this) | set(prev)}
    riser = max(deltas.items(), key=lambda kv: kv[1], default=None)
    faller = min(deltas.items(), key=lambda kv: kv[1], default=None)
    # top new artist: first-seen in this month, ordered by this month's plays
    afp = store.trends.first_play_map("artist")
    new_this = {a: this.get(a, 0) for a, d in afp.items() if month_of(d) == cur["month"]}
    top_new = max(new_this.items(), key=lambda kv: kv[1], default=None)
    top_artists = [{"artist": a, "plays": n, "art": store.charts.artist_thumbnail(a)}
                   for a, n in sorted(this.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    mtp = store.trends.month_track_plays(start, end)
    tk = max(mtp.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    if tk:
        c = store.trends.track_cards([tk[0]]).get(tk[0], {})
        top_track = {"track": tk[0], "title": c.get("title"), "artist": c.get("artist", ""),
                     "plays": tk[1], "art": c.get("thumbnail")}
    else:
        top_track = None
    return {"month": cur["month"], "plays": cur["plays"], "listen_days": cur["listen_days"],
            "longest_streak": cur["longest_streak"],
            "top_new_artist": ({"artist": top_new[0], "plays": top_new[1]}
                               if top_new and top_new[1] > 0 else None),
            "riser": ({"artist": riser[0], "delta": riser[1]} if riser and riser[1] > 0 else None),
            "faller": ({"artist": faller[0], "delta": faller[1]} if faller and faller[1] < 0 else None),
            "play_days": month_day_counts(day_counts, cur["month"]) if day_counts else [],
            "first_weekday": (datetime(y, mo, 1, tzinfo=timezone.utc).weekday() + 1) % 7,
            "top_artists": top_artists, "top_track": top_track,
            "binge": month_peak_day(day_counts, meta, cur["month"]) if day_counts else None}


_STALE_BUCKETS = (("played <30d", 30), ("30-90d", 90), ("90-365d", 365), (">365d", None))


def health_snapshot(store, now):
    """Library-health readout (#80): never-played share, staleness histogram, dead-weight playlists."""
    total, never = store.trends.never_played()
    counts = {label: 0 for label, _ in _STALE_BUCKETS}
    counts["never"] = 0
    for _key, last in store.trends.track_last_play():
        if last is None:
            counts["never"] += 1
            continue
        age_days = (now - last) / 86400.0
        for label, cap in _STALE_BUCKETS:
            if cap is None or age_days < cap:
                counts[label] += 1
                break
    staleness = [{"bucket": label, "n": counts[label]} for label, _ in _STALE_BUCKETS]
    staleness.append({"bucket": "never", "n": counts["never"]})
    quiet_before = now - REDISCOVER_QUIET_DAYS * 86400
    rediscover = store.trends.rediscover_tracks(quiet_before, limit=3)
    recency = store.collection.saved_albums_recency()
    saved = {a["browse"]: a for a in store.collection.get_saved_albums()}
    unopened = [saved[b] for b, ts in recency.items() if ts is None and b in saved]
    unopened_albums = {"count": len(unopened),
                       "items": [{"browse_id": a["browse"], "title": a["title"],
                                  "artist": a["artist"], "art": a.get("thumbnail")}
                                 for a in unopened[:3]]}
    return {"total_tracks": total, "never_played": never,
            "never_played_share": (never / total) if total else 0.0,
            "staleness": staleness, "dead_playlists": store.trends.dead_playlists(max_listens=0),
            "rediscover": rediscover, "unopened_albums": unopened_albums}


def _top_families(week, k=TOP_FAMILIES):
    return {f for f, _ in sorted(week["families"].items(), key=lambda kv: (-kv[1], kv[0]))[:k]}


def detect_spotlight(weeks, months, review, floor_day, store, now, days=(), insights=()):
    """Fixed-priority novelty cascade; first match wins. Returns {signature, headline, detail, anchor}
    or None. Editorial: only ONE candidate is stashed per rollup.

    `days` is the full set of UTC days with any listening (build()'s play_day_counts, day-only), used
    by detector 5 for the cross-month-safe current streak. Defaults to () so callers that don't care
    about the streak detector (e.g. tests exercising an earlier detector) need not pass it.

    `insights` (optional, defaults to ()) is the already-ranked+baked list from detect_insights: when
    non-empty its rank-1 entry outranks every other detector (priority 0), anchoring Home's spotlight
    link at the new Insights section instead of one of the legacy macro-trend anchors."""
    if insights:
        top = insights[0]
        return {"signature": top["signature"], "anchor": "insights",
                "headline": top["headline"], "detail": top["detail"]}
    # 1. Month rollover: a new full-month recap became available.
    if review is not None and review["month"] != store.get_setting("trend_spotlight_review_month"):
        return {"signature": f"month_review:{review['month']}", "anchor": "review",
                "headline": "Your month in review is ready",
                "detail": f"{review['plays']} plays across {review['listen_days']} days last month."}
    # uncensored weeks only (drop the left-censored warm-up window)
    unc = [w for w in weeks if floor_day is None or w["week_start_day"] > floor_day + 7]
    # 2. Discovery-rate spike.
    if unc and discovery_spike([w["new_artist_plays"] for w in unc]):
        w = unc[-1]
        return {"signature": f"discovery_spike:{w['week_start_day']}", "anchor": "discovery",
                "headline": "A burst of new artists",
                "detail": f"{w['new_artist_plays']} of this week's plays went to artists new to you."}
    # 3. A genre family newly in the top 5.
    if len(unc) >= 2:
        entered = _top_families(unc[-1]) - _top_families(unc[-2])
        if entered:
            fam = sorted(entered)[0]
            return {"signature": f"family_enter:{fam}:{unc[-1]['week_start_day']}", "anchor": "listening",
                    "headline": f"{fam} is climbing", "detail": f"{fam} just entered your weekly top 5."}
    # 4. Diversity 90-day extreme.
    floor_month = month_of((now // 86400) - DIVERSITY_WINDOW_DAYS) if now is not None else None
    win = [m for m in months if floor_month is None or m["month"] >= floor_month]
    if len(win) >= DIVERSITY_MIN_MONTHS:
        cur = win[-1]
        divs = [m["diversity"] for m in win]
        if cur["diversity"] == max(divs) and cur["diversity"] > min(divs):
            return {"signature": f"diversity_high:{cur['month']}", "anchor": "diversity",
                    "headline": "Your listening is at its most varied",
                    "detail": "This month is a 90-day high for genre variety."}
        if cur["diversity"] == min(divs) and cur["diversity"] < max(divs):
            return {"signature": f"diversity_low:{cur['month']}", "anchor": "diversity",
                    "headline": "You're in a groove", "detail": "This month is a 90-day low for genre variety."}
    # 5. Daily-listening streak threshold (largest crossed). Uses the CURRENT ACTIVE streak over the
    # full day set (cross-month), not months[-1]["longest_streak"] (month-bucketed: a streak spanning a
    # month boundary would otherwise be split and under-detected).
    if days:
        streak = current_streak(days)
        crossed = [t for t in STREAK_THRESHOLDS if streak >= t]
        if crossed:
            t = max(crossed)
            return {"signature": f"streak:{t}", "anchor": "review", "headline": f"{t}-day listening streak",
                    "detail": f"You've listened every day for {streak} days running."}
    return None


def _bake_art(store, insights, now):
    """Enrich each ranked insight with display fields: track-subject insights get title/art/album-anchor
    via a batch track_cards lookup; artist-subject insights get an artist thumbnail + artist-page anchor.
    Adds computed_at to every entry.

    repetition and song_of_week's headlines are generic ("A track is hooking in" / "Song of the week")
    until the track's title is known here, so this also folds the title into the headline for those two
    kinds -- both the Insights card and the Home spotlight (trend_spotlight.html) render ONLY headline +
    detail, so a card that never names its subject would otherwise leave the user guessing which track."""
    track_keys = [i["subject"] for i in insights if i["subject_kind"] == "track"]
    cards = store.trends.track_cards(track_keys)
    out = []
    for i in insights:
        if i["subject_kind"] == "track":
            c = cards.get(i["subject"], {})
            artist = i["artist"] or c.get("artist", "")
            title = c.get("title")
            anchor = (f"/album?browse={c['album_browse_id']}" if c.get("album_browse_id")
                      else f"/artist?name={quote(artist)}")
            headline = i["headline"]
            if title and i["kind"] == "repetition":
                headline = f'"{title}" is hooking in'
            elif title and i["kind"] == "song_of_week":
                headline = f"Song of the week: {title}"
            i = {**i, "title": title, "artist": artist, "art": c.get("thumbnail"), "headline": headline,
                 "anchor": anchor, "cta": "Open the album" if c.get("album_browse_id") else "See the artist"}
        else:
            i = {**i, "art": store.charts.artist_thumbnail(i["artist"]),
                 "anchor": f"/artist?name={quote(i['artist'])}", "cta": "Explore the catalog"}
        out.append({**i, "computed_at": now})
    return out


def detect_insights(store, day_counts, meta, artist_first, track_first, now):
    """Run all seven insight detectors, rank them, and bake display art/anchor/cta onto the survivors.
    `day_counts`/`meta`/`artist_first`/`track_first` are build()'s own already-fetched material (no
    extra queries); play_events is read here only for the repetition detector's real-timestamp series."""
    now_day = int(now // 86400)
    floor_day = store.trends.first_play_floor_day()
    f = _fold(day_counts, meta)
    series = {}
    for ev in store.history.play_events_since(0):
        series.setdefault(ev["identity_key"], []).append(ev["played_at"])
    for k in series:
        series[k].sort()
    raw = (detect_emergence(f["artist_week"], artist_first, floor_day, now_day)
           + detect_put_to_bed(f["artist_month"], f["artist_day"], now_day)
           + detect_repetition(series)
           + detect_revival(f["artist_day"], floor_day, now_day)
           + detect_one_song(f["track_day"], meta)
           + detect_binge(f["day_total"], f["day_artist"], now_day)
           + detect_song_of_week(f["track_week"], now_day))
    return _bake_art(store, rank_insights(raw, now_day), now)


def build(store, now) -> dict:
    """Precompute the whole Trends rollup + spotlight candidate and materialize it. Rec-worker only."""
    _build_first_play_index(store)
    day_counts = store.trends.play_day_counts()
    meta = store.trends.track_meta()
    artist_first = store.trends.first_play_map("artist")
    track_first = store.trends.first_play_map("track")
    weeks = compute_weeks(day_counts, meta, artist_first, track_first)
    months = compute_months(day_counts, meta, artist_first)
    insights = detect_insights(store, day_counts, meta, artist_first, track_first, now)
    review = month_review(months, store, now, day_counts, meta)
    floor_day = store.trends.first_play_floor_day()
    days = {day for day, _key, _cnt in day_counts}
    spotlight = detect_spotlight(weeks, months, review, floor_day, store, now, days=days, insights=insights)
    payload = {"built_at": now, "first_play_floor_day": floor_day, "weeks": weeks, "months": months,
               "insights": insights, "review": review, "health": health_snapshot(store, now),
               "spotlight": spotlight}
    store.put_proposals("trend_rollups", payload, now)
    # Only mark the review month "consumed" when its month-rollover candidate is the one that actually
    # won the spotlight cascade. detect_spotlight lets insights (priority 0) outrank the month-rollover
    # detector, so on a week where an insight fires, the review would otherwise be silently burned
    # without ever reaching Home -- the review keeps re-competing for the spotlight on every subsequent
    # build until a build finally has no higher-priority insight to beat it.
    if (review is not None and spotlight is not None
            and spotlight["signature"] == f"month_review:{review['month']}"):
        store.set_setting("trend_spotlight_review_month", review["month"])
    return payload


def _latest_full_week_start(week_keys, now_day):
    full = [w for w in sorted(week_keys) if w + 7 <= now_day]     # exclude the in-progress week
    return full[-1] if full else None


def _month_succ(m):
    y, mo = (int(x) for x in m.split("-"))
    return f"{y + (mo == 12):04d}-{(mo % 12) + 1:02d}"


def _fold(day_counts, meta):
    """One pass -> the per-dimension count structures the detectors need."""
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


def detect_emergence(artist_week, artist_first, floor_day, now_day):
    out = []
    for artist, weeks in artist_week.items():
        fd = artist_first.get(artist)
        if fd is None or (floor_day is not None and fd <= floor_day + FLOOR_CENSOR_DAYS):
            continue                                          # censored: no genuine prior, not "new"
        if now_day - fd > EMERGENCE_MAX_AGE_DAYS:
            continue
        lw = _latest_full_week_start(weeks.keys(), now_day)
        if lw is None or weeks.get(lw, 0) < EMERGENCE_MIN_LATEST:
            continue
        nonzero = [w for w in sorted(weeks) if weeks[w] > 0]
        base = weeks[nonzero[0]]
        latest = weeks[lw]
        if nonzero[0] == lw or latest < EMERGENCE_GROWTH * base:
            continue
        wk_ago = (lw - nonzero[0]) // 7
        out.append({"kind": "emergence", "subject_kind": "artist", "subject": artist,
                    "artist": artist, "title": None, "event_day": lw,
                    "magnitude": latest / EMERGENCE_MIN_LATEST, "signature": f"emergence:{artist}:{lw}",
                    "headline": f"{artist} is taking off",
                    "detail": f"{wk_ago} week{'s' if wk_ago != 1 else ''} ago you tried them {base} time"
                              f"{'s' if base != 1 else ''}. Last week: {latest} plays.",
                    "evidence": [{"k": "first week", "v": f"{base} play{'s' if base != 1 else ''}"},
                                 {"k": "last week", "v": f"{latest} plays"}]})
    return out


def detect_put_to_bed(artist_month, artist_day, now_day):
    out = []
    for artist, months in artist_month.items():
        # Track the LAST run of consecutive qualifying months (not the longest ever): the "then quiet"
        # copy is a temporal-adjacency claim, so the cited run must be the one immediately preceding the
        # silence, not whichever run happens to be historically longest. `run` is the run in progress;
        # `last_qual` is overwritten every time `run` reaches PTB_MONTHS, so it always ends up holding
        # the most recent (chronologically last) run that ever qualified -- which, since no months exist
        # in `months` after the artist goes silent, is exactly the run that immediately precedes the gap.
        run, last_qual = [], []
        for m in sorted(months):
            if months[m] >= PTB_MONTHLY_MIN:
                run = run + [m] if (run and _month_succ(run[-1]) == m) else [m]
            else:
                run = []
            if len(run) >= PTB_MONTHS:
                last_qual = list(run)
        if len(last_qual) < PTB_MONTHS:
            continue
        days = artist_day.get(artist, {})
        last_play = max(days) if days else None
        if last_play is None or now_day - last_play < PTB_SILENCE_DAYS:
            continue
        # peak drives the magnitude (how big the run was); the copy's "N+ plays a month" claim must
        # instead cite the run's MINIMUM month, since that's the only figure every cited month actually
        # cleared -- citing the peak would overstate months in the run below it (e.g. a 10/8 run is
        # honestly "8+ plays a month", not "10+", since February only cleared 8).
        peak = max(months[m] for m in last_qual)
        floor_plays = min(months[m] for m in last_qual)
        wks = (now_day - last_play) // 7
        out.append({"kind": "put_to_bed", "subject_kind": "artist", "subject": artist,
                    "artist": artist, "title": None, "event_day": last_play + PTB_SILENCE_DAYS,
                    "magnitude": peak / PTB_MONTHLY_MIN, "signature": f"put_to_bed:{artist}:{last_play}",
                    "headline": f"You put {artist} to bed",
                    "detail": f"{floor_plays}+ plays a month, then quiet for {wks} weeks.",
                    "evidence": [{"k": "sustained", "v": f"{len(last_qual)} months"},
                                 {"k": "silent", "v": f"{wks} weeks"}]})
    return out


def detect_repetition(play_series):
    """play_series = {track_key: [played_at_secs asc]} from play_events ONLY (real timestamps)."""
    out = []
    for key, ts in play_series.items():
        if len(ts) < REPETITION_MIN_RETURNS + 1:
            continue
        gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        tail = gaps[-REPETITION_MIN_RETURNS:]
        if not all(tail[i] > tail[i + 1] for i in range(len(tail) - 1)) or tail[-1] <= 0:
            continue
        event_day = int(ts[-1] // 86400)
        days = [max(1, round(g / 86400.0)) for g in tail]
        out.append({"kind": "repetition", "subject_kind": "track", "subject": key,
                    "artist": None, "title": None, "event_day": event_day,
                    "magnitude": tail[0] / tail[-1], "signature": f"repetition:{key}:{event_day}",
                    "headline": "A track is hooking in",
                    "detail": "You keep coming back faster: " + " -> ".join(f"{d}d" for d in days) + ".",
                    "evidence": [{"k": "returns", "v": " -> ".join(f"{d}d" for d in days)}]})
    return out


def detect_revival(artist_day, floor_day, now_day):
    out = []
    if floor_day is None:
        return out
    max_day = max((d for days in artist_day.values() for d in days), default=None)
    if max_day is None or max_day - floor_day < REVIVAL_DORMANT_DAYS + REVIVAL_RECENT_DAYS:
        return out                                            # can't even observe a 6-month gap
    win_start = now_day - REVIVAL_RECENT_DAYS + 1
    for artist, days in artist_day.items():
        recent = sum(n for d, n in days.items() if d >= win_start)
        prior = [d for d in days if d < win_start]
        if recent < REVIVAL_RECENT_MIN or not prior:
            continue
        gap = win_start - max(prior)                          # zero-play stretch before the burst
        if gap < REVIVAL_DORMANT_DAYS:
            continue
        months = gap // 30
        out.append({"kind": "revival", "subject_kind": "artist", "subject": artist,
                    "artist": artist, "title": None, "event_day": now_day,
                    "magnitude": recent / REVIVAL_RECENT_MIN,
                    "signature": f"revival:{artist}:{week_start(now_day)}",
                    "headline": f"{artist} is back",
                    "detail": f"Dormant {months} months, back with {recent} plays this week.",
                    "evidence": [{"k": "dormant", "v": f"{months} months"},
                                 {"k": "this week", "v": f"{recent} plays"}]})
    return out


def detect_one_song(track_day, meta):
    artist_tracks = {}
    for key, (artist, _g) in meta.items():
        if artist:
            artist_tracks.setdefault(artist, []).append(key)
    out = []
    for artist, keys in artist_tracks.items():
        plays = {k: sum(track_day.get(k, {}).values()) for k in keys}
        nonzero = [k for k in keys if plays[k] > 0]
        if len(nonzero) != 1 or plays[nonzero[0]] < ONE_SONG_MIN_PLAYS:
            continue
        k = nonzero[0]
        out.append({"kind": "one_song", "subject_kind": "track", "subject": k,
                    "artist": artist, "title": None, "event_day": max(track_day.get(k, {0: 0})),
                    "magnitude": plays[k] / ONE_SONG_MIN_PLAYS, "signature": f"one_song:{artist}:{k}",
                    "headline": f"One {artist} song on repeat",
                    "detail": f"{plays[k]} plays, all on a single track. Explore the rest of their catalog.",
                    "evidence": [{"k": "plays", "v": f"{plays[k]} on 1 track"}]})
    return out


def detect_binge(day_total, day_artist, now_day):
    listen_days = sorted(day_total)
    for day in reversed(listen_days):
        prior = [d for d in listen_days if d < day]
        if len(prior) < BINGE_MIN_PRIOR_DAYS:
            continue
        base = _median([day_total[d] for d in prior[-BINGE_TRAILING_DAYS:]])
        if base <= 0 or day_total[day] < BINGE_RATIO * base:
            continue
        arts = day_artist.get(day, {})
        if not arts:
            continue
        top_artist, top_n = max(arts.items(), key=lambda kv: (kv[1], kv[0]))
        pct = top_n / day_total[day]
        if pct < BINGE_CONCENTRATION:
            continue
        # Spec Detail template: "{Weekday Mon D}: {n} plays, {pct}% {artist}." `day` is a UTC day number
        # (day = int(ts // 86400)), so day*86400 is its UTC midnight; render with the same
        # fromtimestamp(day * 86400, tz=timezone.utc) convention used elsewhere in this module (e.g.
        # month_of) and the same "%b %-d" (no leading zero) convention web/routes/trends.py already uses
        # for day-anchored labels, prefixed with "%a" for the weekday.
        when = datetime.fromtimestamp(day * 86400, tz=timezone.utc).strftime("%a %b %-d")
        return [{"kind": "binge", "subject_kind": "artist", "subject": top_artist,
                 "artist": top_artist, "title": None, "event_day": day,
                 "magnitude": day_total[day] / (BINGE_RATIO * base), "signature": f"binge:{day}",
                 "headline": "A binge day",
                 "detail": f"{when}: {day_total[day]} plays, {round(pct * 100)}% {top_artist}.",
                 "evidence": [{"k": "plays", "v": f"{day_total[day]} in a day"},
                              {"k": "focus", "v": f"{round(pct * 100)}% {top_artist}"}]}]
    return []


def detect_song_of_week(track_week, now_day):
    lw = _latest_full_week_start(track_week.keys(), now_day)
    if lw is None or not track_week.get(lw):
        return []
    key, n = max(track_week[lw].items(), key=lambda kv: (kv[1], kv[0]))
    if n < SOTW_MIN_PLAYS:
        return []
    # `lw` is the latest FULL week, i.e. the previous, already-concluded week -- not the in-progress
    # current one (_latest_full_week_start excludes that) -- so the copy must say "last week", not the
    # (currently ongoing) "this week".
    return [{"kind": "song_of_week", "subject_kind": "track", "subject": key,
             "artist": None, "title": None, "event_day": lw + 6, "magnitude": n / SOTW_MIN_PLAYS,
             "signature": f"song_of_week:{lw}:{key}", "headline": "Song of the week",
             "detail": f"{n} plays last week, your most-played track.",
             "evidence": [{"k": "plays", "v": f"{n} last week"}]}]


def rank_insights(raw, now_day):
    scored = []
    for ins in raw:
        rec = max(0.0, 1.0 - (now_day - ins["event_day"]) / RECENCY_HORIZON_DAYS)
        mag = min(ins["magnitude"], MAGNITUDE_CAP) / MAGNITUDE_CAP
        scored.append({**ins, "score": rec * INSIGHT_RARITY[ins["kind"]] * mag})
    # A fully recency-decayed (>= RECENCY_HORIZON_DAYS old) insight scores exactly 0. Drop those before
    # truncating: they must never render as a card, and (since insights[0] doubles as the Home spotlight
    # candidate) a stale insight must never win the spotlight just because nothing fresher fired.
    scored = [s for s in scored if s["score"] > 0]
    scored.sort(key=lambda i: (-i["score"], i["signature"]))
    return scored[:INSIGHTS_MAX]
