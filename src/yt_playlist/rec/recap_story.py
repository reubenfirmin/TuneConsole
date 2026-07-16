"""#79 Monthly recap story ("Your Month") -- turn a completed month's rollup into a sequence of
narrative BEATS for the full-screen reel.

Design: docs/superpowers/specs/2026-07-10-trends-monthly-recap-story-design.md

The beat builders are PURE: each takes already-prepared month data and returns a beat dict (rendered
verbatim by the route) or None (omitted -- a quiet month simply runs shorter). `build_story` wires the
store data in and assembles them; `personality` is the one beat that reads the store (acoustic + hours).
Nothing here renders HTML."""
from datetime import datetime, timezone

from yt_playlist.util import genre_map

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
_MIN_ACOUSTIC = 12   # need at least this many acoustic-tagged plays before we claim "you PLAYED ..."

# Archetype names by the sign of each axis around 0.5: (energy_charged, exploring, night_owl).
_ARCHETYPES = {
    (True,  True,  True):  ("Midnight Voyager", "charged, wide-ranging, and wide awake after dark"),
    (True,  True,  False): ("Daylight Trailblazer", "high-energy and always chasing something new, in the light"),
    (True,  False, True):  ("Nocturnal Loyalist", "you went hard on your favorites, deep into the night"),
    (True,  False, False): ("Power-Hour Regular", "high-energy comfort listening on your own clock"),
    (False, True,  True):  ("Midnight Explorer", "mellow, curious, and most alive after dark"),
    (False, True,  False): ("Daydream Wanderer", "easygoing and exploratory through the daylight hours"),
    (False, False, True):  ("Late-Night Comfort", "calm, familiar, and yours after hours"),
    (False, False, False): ("Steady Companion", "calm and loyal -- the same trusted rotation, on repeat"),
}

# aura hue borrows the genre-family colours the app already uses (kept in sync with app.css .fam-N).
_FAMILY_HUE = {"house": "#8b7cff", "electro-synth": "#57a6f5", "techno": "#46e2b0",
               "trance": "#bd91ff", "ambient": "#4fd6e0"}
_DEFAULT_HUE = "#4fd6e0"


def _month_bounds(month):
    """'YYYY-MM' -> (since_ts, until_ts) UTC for that calendar month."""
    y, m = (int(x) for x in month.split("-"))
    since = datetime(y, m, 1, tzinfo=timezone.utc).timestamp()
    until = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    return since, until


def _clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def energy_axis(track_plays, audio, meta):
    """Calm(0)..Charged(1) for the month. Weighted by plays over tracks that HAVE acoustic energy; where
    coverage is thin, blends in genre-family priors so the axis is always defined. Returns
    (value, source) where source is 'acoustic' (mostly measured) or 'genre' (mostly inferred)."""
    e_num = e_den = 0.0
    covered = 0
    g_num = g_den = 0.0
    for key, plays in track_plays.items():
        a = audio.get(key)
        if a and a.get("energy") is not None:
            e_num += a["energy"] * plays
            e_den += plays
            covered += plays
        genre = (meta.get(key) or ("", None))[1]
        fam = genre_map.family(genre) if genre else None
        g_num += _FAMILY_ENERGY.get(fam, _DEFAULT_ENERGY) * plays
        g_den += plays
    acoustic = (e_num / e_den) if e_den else None
    genre = (g_num / g_den) if g_den else _DEFAULT_ENERGY
    if acoustic is None:
        return _clamp01(genre), "genre"
    # blend acoustic with genre by how much of the month acoustic actually covered
    total = sum(track_plays.values()) or 1
    w = min(1.0, covered / total)
    return _clamp01(acoustic * w + genre * (1 - w)), ("acoustic" if covered >= _MIN_ACOUSTIC else "genre")


def rhythm_axis(hours):
    """Daylight(0)..Night-owl(1). Share of plays in the 21:00-05:59 window (UTC buckets)."""
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


def personality(store, month, month_row, meta):
    """The signature beat: 3 axes -> a named archetype + a deterministic aura. Always defined (energy
    falls back to genre priors, exploration to the month rollup, rhythm to play timestamps)."""
    since, until = _month_bounds(month)
    track_plays = store.trends.ledger_track_plays(since, until)
    audio = store.trends.track_audio()
    hours = store.trends.play_hours(since, until)
    energy, e_src = energy_axis(track_plays, audio, meta)
    rhythm = rhythm_axis(hours)
    explore = exploration_axis(month_row or {})
    name, blurb = _ARCHETYPES[(energy >= 0.5, explore >= 0.5, rhythm >= 0.5)]
    # dominant genre family this month (for the aura hue), from the month's per-key plays
    fam_plays = {}
    for key, plays in track_plays.items():
        genre = (meta.get(key) or ("", None))[1]
        fam = genre_map.family(genre) if genre else None
        if fam:
            fam_plays[fam] = fam_plays.get(fam, 0) + plays
    top_fam = max(fam_plays, key=fam_plays.get) if fam_plays else None
    return {
        "kind": "personality", "name": name, "blurb": blurb,
        "axes": {"energy": round(energy, 3), "exploration": round(explore, 3), "rhythm": round(rhythm, 3)},
        "energy_source": e_src, "top_family": top_fam,
        "aura": {"hue": _FAMILY_HUE.get(top_fam, _DEFAULT_HUE),
                 "turbulence": round(energy, 3), "reach": round(explore, 3), "warmth": round(rhythm, 3)},
    }


# ── beat builders (pure) ─────────────────────────────────────────────────────────────────────
def _beat_numbers(review, month_row):
    return {"kind": "numbers", "plays": review["plays"], "listen_days": review["listen_days"],
            "streak": review["longest_streak"],
            "distinct_artists": (month_row or {}).get("distinct_artists")}


ALL_IN_MIN_SHARE = 0.10    # "all in on X" only when the top artist genuinely dominated (>=10% of plays)
WIDE_NET_MIN_ARTISTS = 40  # ...else, a month spread thin across many artists tells THAT story instead
WIDE_NET_MIN_RATIO = 0.35  # distinct artists / plays
NEW_OBSESSION_MIN = 4      # a "new obsession" needs real repetition, not one curious play


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


def _beat_new_obsession(review):
    n = review.get("top_new_artist")
    if not n or n.get("plays", 0) < NEW_OBSESSION_MIN:
        return None
    return {"kind": "new", "artist": n["artist"], "plays": n["plays"]}


def _beat_diversity(months, month):
    idx = next((i for i, x in enumerate(months) if x["month"] == month), None)
    if idx is None or idx == 0:
        return None
    cur, prev = months[idx].get("diversity", 0.0), months[idx - 1].get("diversity", 0.0)
    delta = cur - prev
    if abs(delta) < 0.03:
        return None
    return {"kind": "diversity", "direction": "branched out" if delta > 0 else "narrowed in",
            "cur": round(cur, 2), "prev": round(prev, 2), "delta": round(delta, 2)}


def _beat_revival(insights):
    rev = next((i for i in (insights or []) if i.get("kind") == "revival"), None)
    if not rev:
        return None
    return {"kind": "revival", "artist": rev.get("artist") or rev.get("subject"),
            "detail": rev.get("detail")}


def _cover(review, month_name, beats, personality_beat):
    """The opening card: a thesis auto-picked from the strongest available beat."""
    kinds = {b["kind"] for b in beats}
    if "all_in" in kinds:
        thesis = "The month you went all in."
    elif "wide_net" in kinds:
        thesis = "The month you cast a wide net."
    elif "new" in kinds:
        thesis = "The month something new took hold."
    elif "diversity" in kinds:
        thesis = "The month your taste shifted."
    else:
        thesis = "Your month, replayed."
    return {"kind": "cover", "month_name": month_name, "plays": review.get("plays", 0),
            "thesis": thesis, "personality": personality_beat.get("name")}


def _closing(review, month_name, personality_beat):
    top = (review.get("top_artists") or [None])[0]
    return {"kind": "closing", "month_name": month_name, "plays": review.get("plays", 0),
            "personality": personality_beat.get("name"), "aura": personality_beat.get("aura"),
            "top_artist": top["artist"] if top else None,
            "listen_days": review.get("listen_days", 0)}


def build_story(store, months, review, insights, month_name):
    """Assemble a completed month's recap. Returns None when there is no completed month to recap.
    `month_name` is the display name (e.g. 'June'); callers pass their own formatter so tz/locale stay
    in one place."""
    if not review:
        return None
    month = review["month"]
    month_row = next((x for x in months if x["month"] == month), None)
    pers = personality(store, month, month_row, store.trends.track_meta())
    middle = [b for b in (
        _beat_numbers(review, month_row),
        _beat_all_in(review),
        _beat_wide_net(review, month_row),      # mutually exclusive with all_in
        _beat_biggest_night(review, month),
        _beat_new_obsession(review),
        _beat_diversity(months, month),
        _beat_revival(insights),
    ) if b]
    beats = [_cover(review, month_name, middle, pers), *middle, pers,
             _closing(review, month_name, pers)]
    return {"month": month, "month_name": month_name, "beats": beats}
