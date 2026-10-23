"""Pure listening insight detection, editorial ranking and spotlight selection.

Thresholds decide when to interrupt the user; they are editorial constants, not model knobs.
Inputs are detached rows/maps from the background rollup's private snapshot. No repository or
artwork lookups belong here.
"""
from datetime import datetime, timezone

from yt_playlist.rec.trend_statistics import (
    current_streak, fold_plays, latest_full_week_start, median, month_of, month_succ, week_start,
)

DISCOVERY_SPIKE_RATIO = 2.0
DISCOVERY_SPIKE_MIN_ABS = 5
DIVERSITY_WINDOW_DAYS = 90
DIVERSITY_MIN_MONTHS = 3
STREAK_THRESHOLDS = (7, 30, 100)
TOP_FAMILIES = 5
FLOOR_CENSOR_DAYS = 7
EMERGENCE_MAX_AGE_DAYS = 42
EMERGENCE_MIN_LATEST = 8
EMERGENCE_GROWTH = 3.0
PTB_MONTHLY_MIN = 8
PTB_MONTHS = 2
PTB_SILENCE_DAYS = 42
REPETITION_MIN_RETURNS = 3
REVIVAL_DORMANT_DAYS = 180
REVIVAL_RECENT_DAYS = 7
REVIVAL_RECENT_MIN = 3
ONE_SONG_MIN_PLAYS = 10
BINGE_RATIO = 3.0
BINGE_TRAILING_DAYS = 28
BINGE_MIN_PRIOR_DAYS = 7
BINGE_CONCENTRATION = 0.50
SOTW_MIN_PLAYS = 5
INSIGHTS_MAX = 5
RECENCY_HORIZON_DAYS = 42
MAGNITUDE_CAP = 4.0
# Editorial surprise priors: we cannot estimate population frequency. Weekly song cards
# are the least surprising; a revival after six quiet months is the most surprising.
INSIGHT_RARITY = {"revival": 1.00, "put_to_bed": 0.90, "emergence": 0.80,
                  "repetition": 0.70, "one_song": 0.60, "binge": 0.50, "song_of_week": 0.35}


def discovery_spike(counts) -> bool:
    """Latest new-artist plays versus the trailing eight weeks, with an absolute floor."""
    if len(counts) < 2:
        return False
    base = median(counts[-9:-1])
    return counts[-1] >= DISCOVERY_SPIKE_RATIO * base and counts[-1] >= DISCOVERY_SPIKE_MIN_ABS


def _top_families(week, k=TOP_FAMILIES):
    return {f for f, _ in sorted(week["families"].items(), key=lambda kv: (-kv[1], kv[0]))[:k]}


def detect_spotlight(weeks, months, review, floor_day, review_month, now, days=(), insights=()):
    """Fixed-priority cascade: ranked insight, unconsumed recap, discovery, family, variety, streak.

    The full listen-day set preserves streaks that cross calendar month boundaries. The caller
    supplies the already-consumed review month; selection itself never reads or writes settings.
    """
    if insights:
        top = insights[0]
        return {"signature": top["signature"], "anchor": "insights",
                "headline": top["headline"], "detail": top["detail"]}
    if review is not None and review["month"] != review_month:
        return {"signature": f"month_review:{review['month']}", "anchor": "review",
                "headline": "Your month in review is ready",
                "detail": f"{review['plays']} plays across {review['listen_days']} days last month."}
    unc = [w for w in weeks if floor_day is None or w["week_start_day"] > floor_day + 7]
    if unc and discovery_spike([w["new_artist_plays"] for w in unc]):
        w = unc[-1]
        return {"signature": f"discovery_spike:{w['week_start_day']}", "anchor": "discovery",
                "headline": "A burst of new artists",
                "detail": f"{w['new_artist_plays']} of this week's plays went to artists new to you."}
    if len(unc) >= 2:
        entered = _top_families(unc[-1]) - _top_families(unc[-2])
        if entered:
            fam = sorted(entered)[0]
            return {"signature": f"family_enter:{fam}:{unc[-1]['week_start_day']}", "anchor": "listening",
                    "headline": f"{fam} is climbing", "detail": f"{fam} just entered your weekly top 5."}
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
    if days:
        streak = current_streak(days)
        crossed = [t for t in STREAK_THRESHOLDS if streak >= t]
        if crossed:
            t = max(crossed)
            return {"signature": f"streak:{t}", "anchor": "review", "headline": f"{t}-day listening streak",
                    "detail": f"You've listened every day for {streak} days running."}
    return None


def detect_insights(day_counts, meta, artist_first, floor_day, play_series, now):
    """Rank seven detectors over quarantined daily counts and exact timestamp series."""
    now_day = int(now // 86400)
    f = fold_plays(day_counts, meta)
    raw = (detect_emergence(f["artist_week"], artist_first, floor_day, now_day)
           + detect_put_to_bed(f["artist_month"], f["artist_day"], now_day)
           + detect_repetition(play_series)
           + detect_revival(f["artist_day"], floor_day, now_day)
           + detect_one_song(f["track_day"], meta)
           + detect_binge(f["day_total"], f["day_artist"], now_day)
           + detect_song_of_week(f["track_week"], now_day))
    return rank_insights(raw, now_day)


def detect_emergence(artist_week, artist_first, floor_day, now_day):
    out = []
    for artist, weeks in artist_week.items():
        fd = artist_first.get(artist)
        if fd is None or (floor_day is not None and fd <= floor_day + FLOOR_CENSOR_DAYS):
            continue  # Left-censored history cannot establish a genuine discovery.
        if now_day - fd > EMERGENCE_MAX_AGE_DAYS:
            continue
        lw = latest_full_week_start(weeks.keys(), now_day)
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
        # Cite the last qualifying run before silence, not the longest historical run.
        run, last_qual = [], []
        for m in sorted(months):
            if months[m] >= PTB_MONTHLY_MIN:
                run = run + [m] if (run and month_succ(run[-1]) == m) else [m]
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
        # Peak controls rank; the copy's per-month claim must hold for every cited month.
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
    """Exact play timestamps only; coarse observations cannot establish shrinking gaps."""
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
        return out  # Insufficient observed history to establish six dormant months.
    win_start = now_day - REVIVAL_RECENT_DAYS + 1
    for artist, days in artist_day.items():
        recent = sum(n for d, n in days.items() if d >= win_start)
        prior = [d for d in days if d < win_start]
        if recent < REVIVAL_RECENT_MIN or not prior:
            continue
        gap = win_start - max(prior)
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
    for key, (artist, _genre) in meta.items():
        if artist:
            artist_tracks.setdefault(artist, []).append(key)
    out = []
    for artist, keys in artist_tracks.items():
        plays = {k: sum(track_day.get(k, {}).values()) for k in keys}
        nonzero = [k for k in keys if plays[k] > 0]
        if len(nonzero) != 1 or plays[nonzero[0]] < ONE_SONG_MIN_PLAYS:
            continue
        key = nonzero[0]
        out.append({"kind": "one_song", "subject_kind": "track", "subject": key,
                    "artist": artist, "title": None, "event_day": max(track_day.get(key, {0: 0})),
                    "magnitude": plays[key] / ONE_SONG_MIN_PLAYS, "signature": f"one_song:{artist}:{key}",
                    "headline": f"One {artist} song on repeat",
                    "detail": f"{plays[key]} plays, all on a single track. Explore the rest of their catalog.",
                    "evidence": [{"k": "plays", "v": f"{plays[key]} on 1 track"}]})
    return out


def detect_binge(day_total, day_artist, now_day):
    listen_days = sorted(day_total)
    for day in reversed(listen_days):
        prior = [d for d in listen_days if d < day]
        if len(prior) < BINGE_MIN_PRIOR_DAYS:
            continue
        base = median([day_total[d] for d in prior[-BINGE_TRAILING_DAYS:]])
        if base <= 0 or day_total[day] < BINGE_RATIO * base:
            continue
        arts = day_artist.get(day, {})
        if not arts:
            continue
        top_artist, top_n = max(arts.items(), key=lambda kv: (kv[1], kv[0]))
        pct = top_n / day_total[day]
        if pct < BINGE_CONCENTRATION:
            continue
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
    lw = latest_full_week_start(track_week.keys(), now_day)
    if lw is None or not track_week.get(lw):
        return []
    key, n = max(track_week[lw].items(), key=lambda kv: (kv[1], kv[0]))
    if n < SOTW_MIN_PLAYS:
        return []
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
    # Zero-score cards are stale; they cannot become the spotlight through lack of competition.
    scored = [s for s in scored if s["score"] > 0]
    scored.sort(key=lambda i: (-i["score"], i["signature"]))
    return scored[:INSIGHTS_MAX]
