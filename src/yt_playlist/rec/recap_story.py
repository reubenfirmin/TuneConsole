"""#79 Monthly recap story ("Your Month") -- turn a completed month's rollup into a sequence of
narrative BEATS for the full-screen reel.

Design: docs/superpowers/specs/2026-07-10-trends-monthly-recap-story-design.md

Beat builders consume supplied month, acoustic, calendar and taxonomy facts.
Quiet months omit unsupported beats. Nothing here acquires inputs or renders HTML."""
from datetime import datetime, timezone

# ── personality axes ─────────────────────────────────────────────────────────────────────────
# Genre-family energy priors (0 calm .. 1 charged), used to fill the energy axis where a played track
# has no acoustic data. Deliberately coarse; it only has to place a family on the calm<->charged line.
_FAMILY_ENERGY = {
    "ambient": 0.15, "classical": 0.15, "jazz": 0.30, "folk-country": 0.30, "blues": 0.35,
    "soul-funk": 0.45, "world-latin": 0.50, "pop": 0.50, "hiphop": 0.55, "rock-indie": 0.55,
    "experimental": 0.55, "rock-post": 0.58, "rock-classic": 0.62, "house": 0.64, "electro-synth": 0.62,
    "trance": 0.70, "breakbeat": 0.74, "garage-bass": 0.74, "techno": 0.80, "punk": 0.82,
    "dnb": 0.86, "metal": 0.86,
}
_DEFAULT_ENERGY = 0.5

# Archetype names by the sign of each axis around 0.5: (energy_charged, exploring, night_owl).
_ARCHETYPES = {
    (True,  True,  True):  "Midnight Voyager",
    (True,  True,  False): "Daylight Trailblazer",
    (True,  False, True):  "Nocturnal Loyalist",
    (True,  False, False): "Power-Hour Regular",
    (False, True,  True):  "Midnight Explorer",
    (False, True,  False): "Daydream Wanderer",
    (False, False, True):  "Late-Night Comfort",
    (False, False, False): "Steady Companion",
}



def _month_bounds(month):
    """UTC timestamp labels for a calendar month's inclusive and exclusive dates."""
    y, m = (int(x) for x in month.split("-"))
    since = datetime(y, m, 1, tzinfo=timezone.utc).timestamp()
    until = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    return since, until


def _clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def energy_axis(track_plays, audio, meta, *, genre_axes):
    """Return the blended energy score, measured play count, and total play count."""
    e_num = g_num = 0.0
    covered = total = 0
    for key, plays in track_plays.items():
        a = audio.get(key)
        if a and a.get("energy") is not None:
            e_num += a["energy"] * plays
            covered += plays
        genre = (meta.get(key) or ("", None))[1]
        fam = genre_axes[genre][0] if genre else None
        g_num += _FAMILY_ENERGY.get(fam, _DEFAULT_ENERGY) * plays
        total += plays
    genre = (g_num / total) if total else _DEFAULT_ENERGY
    if not covered:
        return _clamp01(genre), covered, total
    # blend acoustic with genre by how much of the month acoustic actually covered
    weight = min(1.0, covered / (total or 1))
    return _clamp01((e_num / covered) * weight + genre * (1 - weight)), covered, total


def _describe_energy(energy, covered, total, has_genre_estimate):
    tone = "high-energy" if energy >= 0.5 else "mellow"
    if covered and covered == total:
        return f"The tracks you played leaned {tone}, based on audio measurements."
    if covered and has_genre_estimate:
        return (f"Your listening leaned {tone}, based on genre estimates "
                f"and audio measurements for {covered:,} of {total:,} plays.")
    if covered:
        return f"Audio measurements covered {covered:,} of {total:,} plays."
    if has_genre_estimate:
        return f"Your genres skew {tone}."
    return ""


def rhythm_axis(hours):
    """Daylight(0)..Night-owl(1). Share of local-hour plays in the 21:00-05:59 window."""
    total = sum(hours) or 1
    night = sum(hours[h] for h in list(range(21, 24)) + list(range(0, 6)))
    return _clamp01(night / total)


def exploration_axis(month_row):
    """Loyalist(0)..Explorer(1) from the month's new-artist share and diversity index. Diversity lives
    ~0.5-0.7 for a real library, so it is rescaled onto 0..1 before averaging with the discovery share."""
    plays = month_row.get("plays") or 1
    discovery = (month_row.get("new_artist_plays", 0) / plays)   # ~0..0.3 in practice
    diversity = month_row.get("diversity", 0.0)
    div_scaled = _clamp01((diversity - 0.35) / 0.4)              # 0.35->0, 0.75->1
    disc_scaled = _clamp01(discovery / 0.25)                     # 25% new -> full
    return _clamp01(0.5 * div_scaled + 0.5 * disc_scaled)


def personality(month_row, meta, *, track_plays, audio, hours, genre_axes):
    """The signature beat: 3 axes -> a named archetype + a deterministic aura. Always defined (energy
    falls back to genre priors, exploration to the month rollup, rhythm to play timestamps)."""
    energy, audio_plays, total = energy_axis(track_plays, audio, meta, genre_axes=genre_axes)
    rhythm = rhythm_axis(hours)
    explore = exploration_axis(month_row or {})
    name = _ARCHETYPES[(energy >= 0.5, explore >= 0.5, rhythm >= 0.5)]
    # dominant genre family this month (for the aura hue), from the month's per-key plays
    fam_plays = {}
    for key, plays in track_plays.items():
        genre = (meta.get(key) or ("", None))[1]
        fam = genre_axes[genre][0] if genre else None
        if fam:
            fam_plays[fam] = fam_plays.get(fam, 0) + plays
    top_fam = max(fam_plays, key=fam_plays.get) if fam_plays else None
    has_genre_estimate = any(fam_plays.get(family, 0) > 0 for family in _FAMILY_ENERGY)
    energy_description = _describe_energy(energy, audio_plays, total, has_genre_estimate)
    exploration = "You explored widely" if explore >= 0.5 else "You stuck close to your favorites"
    time = "after dark" if rhythm >= 0.5 else "during the day"
    return {
        "kind": "personality", "name": name, "blurb": f"{energy_description} {exploration}, mostly {time}.".strip(),
        "axes": {"energy": round(energy, 3), "exploration": round(explore, 3), "rhythm": round(rhythm, 3)},
        "audio_plays": audio_plays, "top_family": top_fam,
        # "hue" is a presentation token: the route fills it in from top_family (web/theme owns the
        # actual genre-family-to-CSS-colour mapping; this module stays detached from web).
        "aura": {"turbulence": round(energy, 3), "reach": round(explore, 3), "warmth": round(rhythm, 3)},
    }


# ── beat builders (pure) ─────────────────────────────────────────────────────────────────────
def _beat_numbers(review, month_row):
    return {"kind": "numbers", "plays": review["plays"], "listen_days": review["listen_days"],
            "streak": review["longest_streak"],
            "distinct_artists": (month_row or {}).get("distinct_artists")}


ALL_IN_MIN_SHARE = 0.10    # "all in on X" only when the top artist genuinely dominated (>=10% of plays)
WIDE_NET_MIN_ARTISTS = 40  # ...else, a month spread thin across many artists tells THAT story instead
WIDE_NET_MIN_RATIO = 0.35  # distinct artists / plays


def _beat_all_in(review):
    """Fires ONLY when one artist genuinely dominated. A 2.6%-of-plays 'top' artist is not going all in
    -- that is a wide-net month (see _beat_wide_net); claiming otherwise would be a lie about the data."""
    top = (review.get("top_artists") or [None])[0]
    plays = review.get("plays") or 0
    if not top or not plays:
        return None
    share = top["plays"] / plays
    if share < ALL_IN_MIN_SHARE:
        return None
    return {"kind": "all_in", "artist": top["artist"], "plays": top["plays"], "art": top.get("art"),
            "share": round(share, 3)}


def _beat_wide_net(review, month_row):
    """The honest counterpart to all-in: when no artist dominated and the month sprawled across many
    artists, that spread IS the story."""
    plays = review.get("plays") or 0
    distinct = (month_row or {}).get("distinct_artists") or 0
    top = (review.get("top_artists") or [None])[0]
    top_share = (top["plays"] / plays) if (top and plays) else 1.0
    if top_share >= ALL_IN_MIN_SHARE:
        return None                          # a dominant artist exists -> all-in tells the story
    if not plays or distinct < WIDE_NET_MIN_ARTISTS or distinct / plays < WIDE_NET_MIN_RATIO:
        return None
    return {"kind": "wide_net", "artists": distinct, "plays": plays}


def _beat_biggest_night(review, month):
    b = review.get("binge")
    if not b:
        return None
    # binge["day"] is an absolute epoch-day index (from month_peak_day), not a day-of-month.
    when = datetime.fromtimestamp(int(b["day"]) * 86400, tz=timezone.utc)
    return {"kind": "night", "label": when.strftime("%a %b %-d"), "plays": b["plays"],
            "pct": b["pct"], "artist": b["artist"]}


def listening_summary(plays, listen_days):
    """Describe recorded activity without mistaking artist concentration for heavy usage."""
    play_label = "play" if plays == 1 else "plays"
    day_label = "day" if listen_days == 1 else "days"
    return f"{plays:,} {play_label} across {listen_days} listening {day_label}."


def _cover(review, month_name, personality_beat):
    """The opening card states the month's activity; the following beats tell its story."""
    thesis = listening_summary(review.get("plays", 0), review.get("listen_days", 0))
    return {"kind": "cover", "month_name": month_name, "plays": review.get("plays", 0),
            "thesis": thesis, "personality": personality_beat.get("name")}


def _closing(review, month_name, personality_beat):
    top = (review.get("top_artists") or [None])[0]
    return {"kind": "closing", "month_name": month_name, "plays": review.get("plays", 0),
            "personality": personality_beat.get("name"), "aura": personality_beat.get("aura"),
            "top_artist": top["artist"] if top else None,
            "listen_days": review.get("listen_days", 0)}


def build_story(months, review, month_name, *, personality_beat=None, detail_beats=()):
    """Assemble a completed month's recap. Returns None when there is no completed month to recap.
    `month_name` is the display name (e.g. 'June'); callers pass their own formatter so tz/locale stay
    in one place."""
    if not review:
        return None
    month = review["month"]
    month_row = next((x for x in months if x["month"] == month), None)
    pers = personality_beat
    middle = [b for b in (
        _beat_numbers(review, month_row),
        _beat_all_in(review),
        _beat_wide_net(review, month_row),      # mutually exclusive with all_in
        _beat_biggest_night(review, month),
    ) if b]
    beats = [_cover(review, month_name, pers), *middle, *(detail_beats or []), pers,
             _closing(review, month_name, pers)]
    return {"month": month, "month_name": month_name, "beats": beats}
