"""Track-ordering primitives: arrange a chosen set of tracks into a pleasing sequence.

A leaf of the rec/ layer: depends only on genre_map (genre segue distances) and util.matching.
Journeys depend on ordering; ordering does not depend on journeys
or on recommendation assembly.
"""
import random
from collections import Counter
from functools import lru_cache

from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key
from yt_playlist.rec.artist_variety import members


def _field(item, name):
    """Read a field from either a track dict (DOM/save path) or a ForYouItem (preview path)."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def genre_keys(items):
    return [_field(item, 'key') or identity_key(_field(item, 'title') or '',
                                              _field(item, 'artist') or '') for item in items]


def attach_genres(items, *, genres):
    """Fill each item's genre from the library (by identity_key) so dj_order can do genre segues.

    Works for both ForYouItem objects (preview) and plain track dicts (DOM/save); mutates in place
    and returns `items`. Without this, dj_order sees no genre and collapses to 'shuffle, but space
    same-artist', no genre journey at all (the comfort-playlist bug). Untagged tracks stay '' (the
    segue just can't smooth across them), never an error.

    A genre the item ALREADY carries is kept when the library has none: out-of-corpus tracks (the
    Fresh card's proposals) know their own genre and can never be looked up here, so overwriting
    would throw that away.
    """
    for it, key in zip(items, genre_keys(items)):
        g = genres.get(key) or (_field(it, "genre") or "")
        if isinstance(it, dict):
            it["genre"] = g
        else:
            it.genre = g
    return items


def dj_order(tracks, stickiness=0.0, seed=0, *, feat=None, previous=frozenset()):
    """Order a chosen track set like a DJ. Start from a seeded shuffle, then greedily pick each next
    track by a lexicographic key: (a) never follow a track with the same artist unless every remaining
    track is that artist; (b) among the rest, place the artist with the MOST tracks left first: this
    schedules the heavy hitters early so they can't pile up at the end (the cause of same-artist
    clustering); (c) break ties by a `stickiness`-scaled genre segue (0 ≈ shuffle, 1 = careful genre
    transitions via the genre map). The first song also comes from a most-remaining artist, with
    ties decided by the seeded shuffle. All credited performers participate in spacing.
    Pure; returns a new list that is a permutation of `tracks`. Items may be track dicts or ForYouItem
    objects; both expose 'artist' and 'genre' (run attach_genres first so 'genre' is populated).
    A feature accessor also supports opaque track keys. Features are read once, counts are updated
    incrementally, and each next selection is a linear scan rather than a full remainder sort:
    O(n²) time and O(n) space, with a bounded cache for genre-family distances.
    """
    items = list(tracks)
    if not items or len(items) <= 2 and not previous:
        return items
    rows = []
    for item in items:
        if feat:
            features = feat(item)
            artist, genre = members(features), features.get("genre")
            family = features.get('family',genre_map.family(genre))
        else:
            identities = item.get('artist_ids') if isinstance(item, dict) else getattr(item, 'artist_ids', None)
            artist = members({'artist': _field(item, 'artist'), 'artist_ids': identities})
            genre = _field(item, "genre")
            family = _field(item,'family') or genre_map.family(genre)
        rows.append((item, artist, family))
    rng = random.Random(seed)
    rng.shuffle(rows)
    remaining = Counter(artist for row in rows for artist in row[1])
    weight = lambda artists: max((remaining[artist] for artist in artists), default=0)
    first = min(range(len(rows)), key=lambda i: (bool(rows[i][1] & previous), -weight(rows[i][1])))
    out = [rows.pop(first)]
    remaining.subtract(out[-1][1])
    distance = lru_cache(maxsize=256)(genre_map.family_distance)
    while rows:
        _, last_artist, last_genre = out[-1]

        def score(c):
            _, artist, genre = c
            same = bool(artist & last_artist)
            gd = distance(last_genre, genre) if (last_genre and genre) else 0.0
            seg = stickiness * gd + (1.0 - stickiness) * rng.random()
            # same-artist last (hard), then most-remaining-artist first (anti-pileup), then segue.
            return (same, -weight(artist), seg)

        best = min(range(len(rows)), key=lambda i: score(rows[i]))
        out.append(rows.pop(best))
        remaining.subtract(out[-1][1])
    return [row[0] for row in out]
