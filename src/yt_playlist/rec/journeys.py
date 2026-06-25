"""Playlist 'journeys': intentional orderings for a generated mix (energy arc, eras, deep dive…),
chosen by weighted-random at generation and learnable via feedback.

Pure: `journey_order` takes a `feat(item) -> dict` accessor, so this module needs no Store and is
trivially testable. Only dependency is genre_map (+ a LAZY import of recommend.dj_order for the
within-band artist spacer — lazy to avoid a circular import, since recommend imports this module).
"""
import random

from yt_playlist.util import genre_map

JOURNEYS = ["energy_arc", "warm_up", "wind_down", "smooth_segue", "odyssey",
            "time_machine", "throwback", "deep_dive", "rediscovery", "shuffle"]

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
    "deep_dive": "Favourites first, then deeper cuts",
    "rediscovery": "Leads with long-unplayed tracks",
    "shuffle": "Shuffle, keeping artists apart",
}

JOURNEY_DESCRIPTIONS = {
    "energy_arc": "Eases in, builds to a peak, then winds back down.",
    "warm_up": "Starts mellow and steadily builds energy.",
    "wind_down": "Starts high-energy and gradually calms down.",
    "smooth_segue": "Flows between songs with neighbouring genres.",
    "odyssey": "Hops between contrasting genres for variety.",
    "time_machine": "Oldest tracks first, moving forward in time.",
    "throwback": "Newest tracks first, working back in time.",
    "deep_dive": "Your most-played favourites first, then deeper cuts.",
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


def _space(items, seed):
    """Seeded artist-spacing (+ light genre segue) for a small group, via the existing dj_order
    engine. <=2 items: nothing to space."""
    if len(items) <= 2:
        return list(items)
    from yt_playlist.rec.recommend import dj_order   # lazy: avoid circular import
    return dj_order(items, stickiness=0.4, seed=seed)


def _split_bands(items, value_of, min_bands=1):
    """Sort items with a non-None value ascending and cut into ~_BAND_SIZE contiguous bands.
    Returns (value_bands_low_to_high, none_band). `min_bands` floors the band count — the arc needs
    >=4 bands to rise AND fall."""
    have = [it for it in items if value_of(it) is not None]
    none = [it for it in items if value_of(it) is None]
    have.sort(key=value_of)
    if not have:
        return [], none
    nb = max(min_bands, round(len(have) / _BAND_SIZE))
    nb = max(1, min(nb, len(have)))
    size = len(have) / nb
    bands = [have[round(i * size):round((i + 1) * size)] for i in range(nb)]
    return [b for b in bands if b], none


def _arrange(bands_low_high, direction):
    if direction == "asc":
        return list(bands_low_high)
    if direction == "desc":
        return list(reversed(bands_low_high))
    # arc: even-index bands ascending, then odd-index bands descending -> highest band in the middle
    return bands_low_high[0::2] + bands_low_high[1::2][::-1]


def _axis_order(items, journey, seed, feat):
    key, direction = _AXIS[journey]
    value_of = lambda it: feat(it).get(key)
    # Arc must rise AND fall: <=3 bands degenerates (2 = a ramp, 3 = ends mid). Floor arc at 4 bands
    # so even short mixes form a proper mountain that returns toward low.
    min_bands = 4 if direction == "arc" else 1
    bands, none_band = _split_bands(items, value_of, min_bands)
    bands = _arrange(bands, direction)
    if none_band:
        bands.append(none_band)                       # undated/untagged always trail at the end
    out = []
    for i, band in enumerate(bands):
        spaced = _space(band, seed + i)
        if (out and len(spaced) > 1 and feat(out[-1])["artist"]
                and feat(out[-1])["artist"] == feat(spaced[0])["artist"]):
            spaced[0], spaced[1] = spaced[1], spaced[0]   # seam repair across the band boundary
        out.extend(spaced)
    return out


def _segue_order(items, journey, seed, feat):
    """Greedy genre-segue: start from a seeded shuffle, then repeatedly pick the next track whose
    genre is NEAREST (smooth_segue) or FARTHEST (odyssey) from the previous, avoiding same-artist
    back-to-back unless forced."""
    rng = random.Random(seed)
    items = list(items)
    rng.shuffle(items)
    out = [items.pop(0)]
    smooth = journey == "smooth_segue"
    while items:
        last_g = feat(out[-1])["genre"] or ""
        last_a = feat(out[-1])["artist"]

        def score(c):
            fc = feat(c)
            same = 1 if (fc["artist"] and fc["artist"] == last_a) else 0
            d = genre_map.distance(last_g, fc["genre"] or "")
            return (same, d if smooth else -d, rng.random())

        items.sort(key=score)
        out.append(items.pop(0))
    return out


def journey_order(tracks, journey_key, seed, feat):
    """Order `tracks` per `journey_key`. `feat(item)` -> {artist, genre, energy, decade, plays,
    recency}. Pure given feat; returns a permutation. Unknown keys fall back to a straight shuffle."""
    items = list(tracks)
    if len(items) <= 2:
        return items
    if journey_key in _AXIS:
        return _axis_order(items, journey_key, seed, feat)
    if journey_key in ("smooth_segue", "odyssey"):
        return _segue_order(items, journey_key, seed, feat)
    return _space(items, seed)
