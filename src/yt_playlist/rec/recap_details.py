"""Optional recap screens: evidence thresholds, finding selection, and factual copy.

The repo gathers inputs, recap_metrics measures them, and this module decides what is useful.
All public builders below are pure. build() is the single worker-facing data-loading boundary.
"""
from collections import Counter
from statistics import median
from urllib.parse import quote

from yt_playlist.rec.recap_metrics import baselines, month_bounds, shift_month, typical

MAX_FINDINGS = 3
MIN_BASELINE_MONTHS = 3
SHARE_CHANGE = .15


def new_this_month(data, month):
    since, until = month_bounds(month)
    events = [p for p in data["plays"] if since <= p["played_at"] < until]
    counts = Counter(p["identity_key"] for p in events)
    # A new installation/import must not turn its entire first month into discoveries.
    established = bool(data["first"]) and min(data["first"].values()) <= since - 30 * 86400
    albums = []
    if established:
        for album in data["albums"]:
            keys = album["keys"]
            heard = keys & counts.keys()
            if len(keys) < 4 or len(heard) < 3 or len(heard) / len(keys) < .60:
                continue
            if min((data["first"].get(k, until) for k in keys), default=until) < since:
                continue
            albums.append({"title": album["title"], "subtitle": album["artist"],
                           "detail": f"{len(heard)} of {len(keys)} tracks played",
                           "href": f"/album?browse={quote(album['browse_id'], safe='')}",
                           "heard": len(heard)})
    albums.sort(key=lambda a: (-a["heard"], a["title"]))
    artist_counts = Counter()
    artist_first = {}
    artist_names = {}
    for key, first in data["first"].items():
        artist = data["meta"].get(key, {}).get("artist")
        if not artist:
            continue
        normalized = artist.casefold()
        artist_names.setdefault(normalized, artist)
        artist_first[normalized] = min(artist_first.get(normalized, first), first)
        artist_counts[normalized] += counts[key]
    artists = [{"title": artist_names[a], "detail": f"{n} plays",
                "href": f"/artist?name={quote(artist_names[a], safe='')}"}
               for a, n in sorted(artist_counts.items(), key=lambda pair: (-pair[1], pair[0]))
               if established and n > 3 and artist_first[a] >= since
               and a not in data["familiar_artists"]]
    playlist_plays = Counter(p.get("playlist_ytm_id") for p in events)
    playlists = []
    for p in data["playlists"]:
        if not since <= p["created_at"] < until:
            continue
        songs = p["track_count"]
        plays = playlist_plays[p["ytm_playlist_id"]]
        size = f"{songs:,} song{'s' if songs != 1 else ''} · " if songs is not None else ""
        playlists.append({"title": p["title"] or "Untitled playlist",
                          "detail": f"{size}{plays:,} play{'s' if plays != 1 else ''}",
                          "href": f"https://music.youtube.com/playlist?list={quote(p['ytm_playlist_id'], safe='')}"})
    groups = [{"label": label, "entries": entries[:3], "total": len(entries)}
              for label, entries in (("Albums", albums), ("Playlists", playlists), ("Artists", artists))
              if entries]
    if not groups:
        return None
    return {"kind": "new_this_month", "title": "New this month", "groups": groups}


def _finding(key, category, title, detail, score, support=frozenset()):
    return {"metric": key, "category": category, "title": title, "detail": detail,
            "score": score, "support": support}


def comparison_screens(current, previous):
    """Changes clear both an effect-size floor and 3 median absolute deviations.

    Habits need positive evidence now and in at least 75% of the baseline. A recurring
    artist need not have an identical share every month. Absolute activity and listening
    runs require matching capture; musical history can span imports and live tracking.
    At most one finding per category.
    """
    if len(previous) < MIN_BASELINE_MONTHS or not current.eligible:
        return []
    different, same = [], []
    n = len(previous)
    capture_previous = [p for p in previous if p.source_signature == current.source_signature]

    def scalar(key, category, value, values, title, stable_title, detail, *,
               absolute, relative=.35, stable=.15, positive=0, require_same_capture=False):
        if require_same_capture:
            values = [v for p, v in zip(previous, values)
                      if p.source_signature == current.source_signature]
            if len(values) < MIN_BASELINE_MONTHS:
                return
        center, spread = typical(values)
        delta = abs(value - center)
        if delta >= max(absolute, abs(center) * relative, 3 * spread) and delta > 0:
            different.append(_finding(key, category, title(value > center), detail(center),
                                      delta / max(absolute, abs(center) * relative)))
        elif value > positive and center > positive and delta <= max(absolute / 3, abs(center) * stable):
            consistent = sum(abs(v - center) <= max(absolute / 3, abs(center) * stable) for v in values)
            if consistent / len(values) >= .75:
                same.append(_finding(key, category, stable_title, detail(center), 1))

    scalar("activity", "amount", current.plays / current.calendar_days,
           [p.plays / p.calendar_days for p in previous],
           lambda up: "More music in your days" if up else "A quieter listening month",
           "A steady amount of listening",
           lambda center: f"{current.plays:,} plays this month. {current.plays / current.calendar_days:.1f} a day, "
                          f"compared with your usual {center:.1f}.", absolute=1, positive=.5,
           require_same_capture=True)
    scalar("days", "regularity", current.days / current.calendar_days,
           [p.days / p.calendar_days for p in previous],
           lambda up: "Listening on more days" if up else "Listening on fewer days",
           "A familiar listening routine",
           lambda center: f"You hit play on {current.days} of {current.calendar_days} days. "
                          f"Usually, it's about {center * current.calendar_days:.0f}.",
           absolute=.20, positive=.20, require_same_capture=True)
    scalar("intensity", "amount", current.plays / current.days,
           [p.plays / p.days for p in previous],
           lambda up: "Busier listening days" if up else "Lighter listening days",
           "Your usual pace on listening days",
           lambda center: f"{current.plays / current.days:.1f} plays on the days you listened. "
                          f"Your usual: {center:.1f}.",
           absolute=3, positive=2, require_same_capture=True)
    if (len(capture_previous) >= MIN_BASELINE_MONTHS and len(current.sessions) >= 6
            and all(len(p.sessions) >= 6 for p in capture_previous)):
        scalar("sessions", "regularity", median(current.sessions), [median(p.sessions) for p in previous],
               lambda up: "Longer runs of listening" if up else "Shorter runs of listening",
               "Your usual listening runs",
               lambda center: f"Your listening runs were typically {median(current.sessions):g} songs long. "
                              f"Your usual: {center:g}.", absolute=2, positive=2,
               require_same_capture=True)
    scalar("repetition", "repetition", sum(c for _, c in current.tracks.most_common(5)) / current.plays,
           [sum(c for _, c in p.tracks.most_common(5)) / p.plays for p in previous],
           lambda up: "More repeat favorites" if up else "A wider song rotation",
           "A familiar repeat rotation",
           lambda center: f"Your top five songs made up "
                          f"{sum(c for _, c in current.tracks.most_common(5)) / current.plays:.0%} "
                          f"of your plays. Your usual: {center:.0%}.", absolute=.20, positive=.30)
    if (len(capture_previous) >= MIN_BASELINE_MONTHS
            and all(p.album_covered_plays / p.plays >= .70 for p in [current, *capture_previous])
            and (len(current.album_run_days) >= 3 or all(len(p.album_run_days) >= 3 for p in capture_previous))):
        scalar("album_runs", "albums", current.album_run_plays / current.plays,
               [p.album_run_plays / p.plays for p in previous],
               lambda up: "More album listening" if up else "Less album listening",
               "Albums stayed in the routine",
               lambda center: f"You stuck with an album for {current.album_run_plays / current.plays:.0%} "
                              f"of your listening. Your usual: {center:.0%}.",
               absolute=.20, positive=.20, require_same_capture=True)
    # Only dated, user-created playlists; unknown dates and generated lists never become zeros.
    # Creation is an occasional positive habit, so a lack of creations never earns a stable claim.
    if current.playlists and all(p.playlists for p in previous):
        scalar("playlist_creation", "curation", current.playlists,
               [p.playlists for p in previous],
               lambda up: "More playlists in the making" if up else "Fewer new playlists",
               "You kept making playlists",
               lambda center: f"You made {current.playlists} playlists this month. "
                              f"Your usual: {center:g}.", absolute=3, positive=0)

    for attr, day_attr, category in (("families", "family_days", "genre"),
                                     ("decades", "decade_days", "era"),
                                     ("artists", "artist_days", "artist")):
        counts = getattr(current, attr)
        # Compare shares of ALL plays only when metadata covers at least 70% in every month.
        if any(sum(getattr(p, attr).values()) / p.plays < .70 for p in [current, *previous]):
            continue
        labels = sorted(set(counts).union(*(getattr(p, attr) for p in previous)))
        for label in labels:
            count = counts[label]
            days = len(getattr(current, day_attr).get(label, ()))
            values = [getattr(p, attr)[label] / p.plays for p in previous]
            value = count / current.plays
            center, spread = typical(values)
            prior_supported = sum(getattr(p, attr)[label] >= 8
                                  and len(getattr(p, day_attr).get(label, ())) >= 3 for p in previous)
            delta = value - center
            display = label.replace("-", " ") if category == "genre" else label
            detail = (f"{count} plays over {days} days. {value:.0%} of your listening, "
                      f"compared with your usual {center:.0%}.")
            priority = {"genre": .02, "era": .01, "artist": 0}[category]
            cohort = ({"genre": "family", "era": "decade", "artist": "artist"}[category], label)
            support = set().union(*(p.cohorts.get(cohort, set()) for p in [current, *previous]))
            if abs(delta) >= max(SHARE_CHANGE, 3 * spread):
                if (delta > 0 and count >= 8 and days >= 3) or (delta < 0 and prior_supported / n >= .75):
                    different.append(_finding(f"{category}:{label}", category,
                        f"{'More' if delta > 0 else 'Less'} {display}", detail,
                        abs(delta) / SHARE_CHANGE + priority + (.005 if delta > 0 else 0), support))
            elif count >= 8 and days >= 3 and prior_supported / n >= .75:
                steady = sum(abs(v - center) <= .08 for v in values) / n >= .75
                if category == "artist":
                    same.append(_finding(f"{category}:{label}", category,
                        f"{display} stayed in rotation",
                        f"{count} plays over {days} days. You keep coming back.", 1 + value, support))
                elif value >= .20 and abs(delta) <= .05 and steady:
                    same.append(_finding(f"{category}:{label}", category,
                        f"{display} stayed in rotation", detail, 1 + value + priority, support))

    def select(findings):
        picked, categories, cohorts = [], set(), []
        for item in sorted(findings, key=lambda f: (-f["score"], f["metric"])):
            support = item["support"]
            overlapping = any(len(support & other) / len(support | other) >= .8 for other in cohorts
                              if support and other)
            if item["category"] not in categories and not overlapping:
                picked.append({k: v for k, v in item.items() if k not in {"score", "category", "support"}})
                categories.add(item["category"])
                cohorts.append(support)
            if len(picked) == MAX_FINDINGS:
                break
        return picked

    return [{"kind": kind, "title": title, "findings": findings}
            for kind, title, findings in (
                ("different", "What you did differently", select(different)),
                ("same", "What you did the same", select(same))) if findings]


def screens(data, month):
    new = new_this_month(data, month)
    current, previous = baselines(data, month)
    return ([new] if new else []) + comparison_screens(current, previous)


def build(store, month):
    from yt_playlist.repos.recap import RecapRepo
    since, _ = month_bounds(shift_month(month, -6))
    _, until = month_bounds(month)
    return screens(RecapRepo(store).snapshot(since, until), month)
