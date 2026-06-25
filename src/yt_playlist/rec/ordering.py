"""Track-ordering primitives: arrange a chosen set of tracks into a pleasing sequence.

A leaf of the rec/ layer: depends only on genre_map (genre segue distances), the library DAO (to
fill genres), and util.matching. It must NOT import recommend or journeys, so journeys can use
dj_order without the old circular import. `recommend` re-exports these for existing call sites.
"""
import random
from collections import Counter

from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key
from yt_playlist.rec.rec_dao import RecDao


def _field(item, name):
    """Read a field from either a track dict (DOM/save path) or a ForYouItem (preview path)."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def attach_genres(store, items):
    """Fill each item's genre from the library (by identity_key) so dj_order can do genre segues.

    Works for both ForYouItem objects (preview) and plain track dicts (DOM/save); mutates in place
    and returns `items`. Without this, dj_order sees no genre and collapses to 'shuffle, but space
    same-artist', no genre journey at all (the comfort-playlist bug). Untagged tracks stay '' (the
    segue just can't smooth across them), never an error.
    """
    key_of = {id(it): (_field(it, "key")
                       or identity_key(_field(it, "title") or "", _field(it, "artist") or ""))
              for it in items}
    genres = RecDao(store).track_genres([k for k in key_of.values() if k])
    for it in items:
        g = genres.get(key_of[id(it)], "")
        if isinstance(it, dict):
            it["genre"] = g
        else:
            it.genre = g
    return items


def dj_order(tracks, stickiness=0.0, seed=0):
    """Order a chosen track set like a DJ. Start from a seeded shuffle, then greedily pick each next
    track by a lexicographic key: (a) never follow a track with the same artist unless every remaining
    track is that artist; (b) among the rest, place the artist with the MOST tracks left first: this
    schedules the heavy hitters early so they can't pile up at the end (the cause of same-artist
    clustering); (c) break ties by a `stickiness`-scaled genre segue (0 ≈ shuffle, 1 = careful genre
    transitions via the genre map). Guarantees no back-to-back same artist whenever that's feasible.
    Pure; returns a new list that is a permutation of `tracks`. Items may be track dicts or ForYouItem
    objects; both expose 'artist' and 'genre' (run attach_genres first so 'genre' is populated).
    """
    items = list(tracks)
    if len(items) <= 2:
        return items
    rng = random.Random(seed)
    rng.shuffle(items)
    out = [items.pop(0)]
    while items:
        last = out[-1]
        remaining = Counter(_field(c, "artist") for c in items)

        def score(c):
            same = bool(_field(c, "artist")) and _field(c, "artist") == _field(last, "artist")
            lg, cg = _field(last, "genre") or "", _field(c, "genre") or ""
            gd = genre_map.distance(lg, cg) if (lg and cg) else 0.0
            seg = stickiness * gd + (1.0 - stickiness) * rng.random()
            # same-artist last (hard), then most-remaining-artist first (anti-pileup), then segue.
            return (same, -remaining[_field(c, "artist")], seg)

        items.sort(key=score)
        out.append(items.pop(0))
    return out
