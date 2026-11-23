"""Playlist 'journeys': intentional orderings for a generated mix (energy arc, eras, deep dive…),
chosen by weighted-random at generation and learnable via feedback.

Pure: `journey_order` takes a `feat(item) -> dict` accessor, so this module needs no Store and is
trivially testable. Dependencies are genre_map and ordering.dj_order (the within-band artist spacer);
both are leaf modules, so there is no longer a circular import to dodge.
"""
import random
from collections import Counter
from functools import lru_cache

from yt_playlist.util import genre_map
from yt_playlist.rec.ordering import dj_order
from yt_playlist.rec.artist_variety import members

# The journey keys, in picker order; each maps to an ordering strategy in journey_order below.
JOURNEYS = ["energy_arc", "warm_up", "wind_down", "smooth_segue", "odyssey",
            "time_machine", "throwback", "deep_dive", "rediscovery", "shuffle"]

# Full display names for the generation panel (the short picker hints are in JOURNEY_HINTS below).
JOURNEY_LABELS = {
    "energy_arc": "Energy arc", "warm_up": "Warm-up", "wind_down": "Wind-down",
    "smooth_segue": "Smooth segue", "odyssey": "Odyssey", "time_machine": "Time machine",
    "throwback": "Throwback", "deep_dive": "Deep dive", "rediscovery": "Rediscovery",
    "shuffle": "Straight shuffle",
}

# Short 5–6 word hints for the Clusters DJ-Journey picker (the full ones above are for the panel).
JOURNEY_HINTS = {
    "energy_arc": "Eases up, peaks, winds down",
    "warm_up": "Starts mellow, steadily builds energy",
    "wind_down": "Starts high, gradually calms down",
    "smooth_segue": "Glides between neighbouring genres",
    "odyssey": "Hops between contrasting genres",
    "time_machine": "Oldest first, moving forward",
    "throwback": "Newest first, working backward",
    "deep_dive": "Favorites first, then deeper cuts",
    "rediscovery": "Leads with long-unplayed tracks",
    "shuffle": "Shuffle, keeping artists apart",
}

# One-sentence descriptions shown alongside each journey in the panel.
JOURNEY_DESCRIPTIONS = {
    "energy_arc": "Eases in, builds to a peak, then winds back down.",
    "warm_up": "Starts mellow and steadily builds energy.",
    "wind_down": "Starts high-energy and gradually calms down.",
    "smooth_segue": "Flows between songs with neighbouring genres.",
    "odyssey": "Hops between contrasting genres for variety.",
    "time_machine": "Oldest tracks first, moving forward in time.",
    "throwback": "Newest tracks first, working back in time.",
    "deep_dive": "Your most-played favorites first, then deeper cuts.",
    "rediscovery": "Leads with what you haven't played in the longest.",
    "shuffle": "A straight shuffle that just keeps artists apart.",
}

# axis journeys -> (feat key, direction). direction: "asc" | "desc" | "arc".
_AXIS = {
    "energy_arc":   ("energy", "arc"),
    "warm_up":      ("energy", "asc"),
    "wind_down":    ("energy", "desc"),
    "time_machine": ("decade", "asc"),
    "throwback":    ("decade", "desc"),
    "deep_dive":    ("plays", "desc"),
    "rediscovery":  ("recency", "asc"),
}

_BAND_SIZE = 4   # target tracks per band; a 12-track mix -> ~3 bands


def _space(items, seed, feat, previous=frozenset()):
    """Seeded artist-spacing (+ light genre segue) for a small group, via the existing dj_order
    engine. <=2 items: nothing to space."""
    if len(items) <= 2 and not previous:
        return list(items)
    return dj_order(items, stickiness=0.4, seed=seed, feat=feat, previous=previous)


def _split_bands(items, value_of, min_bands=1):
    """Sort items with a non-None value ascending and cut into ~_BAND_SIZE contiguous bands.
    Returns (value_bands_low_to_high, none_band). `min_bands` floors the band count: the arc needs
    >=4 bands to rise AND fall."""
    have = [it for it in items if value_of(it) is not None]
    none = [it for it in items if value_of(it) is None]
    have.sort(key=value_of)
    if not have:
        return [], none
    nb = max(min_bands, round(len(have) / _BAND_SIZE))
    nb = max(1, min(nb, len(have)))
    size = len(have) / nb
    boundaries = [0]
    for i in range(1, nb):
        end = round(i * size)
        # Equal values carry no journey distinction. Keep their full choice of
        # performers available to the spacer rather than freezing arbitrary ties.
        while end < len(have) and value_of(have[end - 1]) == value_of(have[end]):
            end += 1
        if boundaries[-1] < end < len(have):
            boundaries.append(end)
    boundaries.append(len(have))
    bands = [have[start:end] for start, end in zip(boundaries, boundaries[1:])]
    return [b for b in bands if b], none


def _arrange(bands_low_high, direction):
    if direction == "asc":
        return list(bands_low_high)
    if direction == "desc":
        return list(reversed(bands_low_high))
    # arc: even-index bands ascending, then odd-index bands descending -> highest band in the middle
    return bands_low_high[0::2] + bands_low_high[1::2][::-1]


def _axis_order(items, journey, seed, feat, previous=None):
    key, direction = _AXIS[journey]
    value_of = lambda it: feat(it).get(key)
    # Arc must rise AND fall: <=3 bands degenerates (2 = a ramp, 3 = ends mid). Floor arc at 4 bands
    # so even short mixes form a proper mountain that returns toward low.
    separate_values = previous is not None or (key == "decade" and len(items) == 2)
    min_bands = 4 if direction == "arc" else (len(items) if separate_values else 1)
    bands, none_band = _split_bands(items, value_of, min_bands)
    bands = _arrange(bands, direction)
    if previous is not None and direction != 'arc' and (last := previous.get(key)) is not None:
        # A continuous stream can return to the start of the range when the
        # eligible choices have no values ahead of its current position.
        ahead = (lambda item: value_of(item) >= last) if direction == 'asc' else (
            lambda item: value_of(item) <= last)
        continuing = [matching for band in bands if (matching := [item for item in band if ahead(item)])]
        if continuing:
            bands = continuing + [[item for item in band if not ahead(item)] for band in bands]
    if none_band:
        bands.append(none_band)                       # undated/untagged always trail at the end
    out = []
    for i, band in enumerate(band for band in bands if band):
        spaced = _space(band, seed + i, feat, members(feat(out[-1]) if out else previous or {}))
        out.extend(spaced)
    return out


def _segue_order(items, journey, seed, feat, previous=None):
    """Greedy genre-segue: start from a seeded shuffle, then repeatedly pick the next track whose
    genre is NEAREST (smooth_segue) or FARTHEST (odyssey) from the previous, avoiding same-artist
    back-to-back unless forced. Detached artist/family rows and a bounded distance cache avoid
    repeating feature resolution. Linear minimum selection keeps this O(n²), with O(n) space."""
    rng = random.Random(seed)
    rows = []
    for item in items:
        features = feat(item)
        rows.append((item, members(features), features.get('family',genre_map.family(features["genre"]))))
    rng.shuffle(rows)
    remaining = Counter(artist for row in rows for artist in row[1])
    weight = lambda artists: max((remaining[artist] for artist in artists), default=0)
    if previous is None:
        first = max(range(len(rows)), key=lambda i: weight(rows[i][1]))
        out = [rows.pop(first)]
        remaining.subtract(out[-1][1])
    else:
        out = [(None, members(previous), previous.get('family') or genre_map.family(previous.get('genre')))]
    smooth = journey == "smooth_segue"
    distance = lru_cache(maxsize=256)(genre_map.family_distance)
    while rows:
        _, last_a, last_g = out[-1]

        def score(c):
            # Sort key, ascending: same-artist last (0 before 1), then nearest genre for smooth_segue
            # (smallest distance first) or farthest for odyssey (negate), with a seeded random tiebreak.
            _, artist, genre = c
            same = bool(artist & last_a)
            d = distance(last_g, genre)
            return (same, -weight(artist), d if smooth else -d, rng.random())

        best = min(range(len(rows)), key=lambda i: score(rows[i]))
        out.append(rows.pop(best))
        remaining.subtract(out[-1][1])
    return [row[0] for row in (out[1:] if previous is not None else out)]


def _road_trip_order(items, seed, feat):
    """Interleave 'mine' and 'theirs' tracks proportional to their pool sizes (already drawn to the
    target ratio upstream, so no ratio parameter is needed here), then repair artist-adjacency in
    small SEQUENTIAL bands — mirroring _axis_order's per-band _space use, NOT a single _space call
    over the whole sequence. _space/dj_order starts with an unconditional shuffle of its entire
    input, so calling it once over the full interleaved list would re-shuffle from scratch and
    destroy the ratio interleaving just built. Banding keeps each band's position in the overall
    sequence fixed and only reorders the handful of tracks within it (same seam-repair pattern as
    _axis_order above)."""
    rng = random.Random(seed)
    mine = [it for it in items if feat(it).get("source") == "mine"]
    theirs = [it for it in items if feat(it).get("source") != "mine"]
    rng.shuffle(mine)
    rng.shuffle(theirs)
    seq = []
    m_used = t_used = 0
    for _ in range(len(items)):
        m_share = (m_used / len(mine)) if mine else 1.0
        t_share = (t_used / len(theirs)) if theirs else 1.0
        if mine and (not theirs or m_used == 0 or m_share <= t_share):
            seq.append(mine[m_used]); m_used += 1
        else:
            seq.append(theirs[t_used]); t_used += 1

    bands = [seq[i:i + _BAND_SIZE] for i in range(0, len(seq), _BAND_SIZE)]
    out = []
    for i, band in enumerate(bands):
        spaced = _space(band, seed + i, feat, members(feat(out[-1])) if out else frozenset())
        out.extend(spaced)
    return out


def journey_order(tracks, journey_key, seed, feat, *, previous=None):
    """Order `tracks` per `journey_key`. `feat(item)` -> {artist, genre, energy, decade, plays,
    recency}. Pure given feat; returns a permutation. Unknown keys fall back to a straight shuffle.
    For a live stream, previous supplies the preceding track's features for transitions.

    'road_trip' is a special case: it is NOT in JOURNEYS (see module docs above), so it never
    appears in a picker, but is dispatched here so rec/road_trip.py can call it directly."""
    items = list(tracks)
    if not items or len(items) <= 2 and previous is None:
        if len(items) == 2 and journey_key in ("time_machine", "throwback"):
            return _axis_order(items, journey_key, seed, feat)
        return items
    features = {id(item): dict(feat(item)) for item in items}
    feat = lambda item: features[id(item)]
    if journey_key == "road_trip":
        return _road_trip_order(items, seed, feat)
    if journey_key in _AXIS:
        return _axis_order(items, journey_key, seed, feat, previous)
    if journey_key in ("smooth_segue", "odyssey"):
        return _segue_order(items, journey_key, seed, feat, previous)
    return _space(items, seed, feat, members(previous or {}))


def continuation_choice(items, style, seed, feat, *, previous, position, span):
    """Pick from eligible, competitively ranked choices at a live stream position."""
    if style == 'energy_arc':
        supported = [item for item in items if feat(item)['energy'] is not None]
        if not supported:
            return items[0]
        ordered = journey_order(supported, style, seed, feat, previous=previous or {})
        return ordered[round((len(ordered) - 1) * (position % span) / (span - 1))]
    return journey_order(items, style, seed, feat, previous=previous or {})[0]
