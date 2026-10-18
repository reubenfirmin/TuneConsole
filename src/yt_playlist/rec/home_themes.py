"""Explicit genre requests for the current Home menu, separate from learned preferences."""
from copy import copy
from collections import Counter, defaultdict

from yt_playlist.rec.ordering import _field, attach_genres
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util import genre_map


def other_genres(store, items, avoid):
    return matching_genres(store, items, avoid=avoid)


def matching_genres(store, items, *, include=(), avoid=()):
    """Keep tracks with a known genre outside the offered families. Resolve current library
    metadata as rendering does, then use the description's artist fallback for untagged
    discoveries. Unknown metadata cannot establish a new genre."""
    if not include and not avoid:
        return items
    # Bundles can predate enrichment. Rendering gives library genres precedence, so
    # filter those same genres on copies instead of changing the cached candidates.
    items = resolved_genres(store, items)
    return [item for item in items if _field(item, "genre")
            and genre_map.family(_field(item, "genre")) not in avoid
            and (not include or genre_map.family(_field(item, "genre")) in include)]


def resolved_genres(store, items):
    """Copy candidates and attach the same current/fallback genres used in descriptions."""
    items = attach_genres(store, [copy(item) for item in items])
    by_artist = None
    for item in items:
        genre = (_field(item, "genre") or "").strip()
        if not genre:
            if by_artist is None:
                by_artist = store.artist_genre_years()
            genre = (by_artist.get(_field(item, "artist") or "") or {}).get("genre") or ""
            if isinstance(item, dict):
                item["genre"] = genre
            else:
                item.genre = genre
    return items


def genre_options(store):
    """Offer genre families present in prepared candidates, using current track metadata.
    Deduplicate tracks across modes/lanes so a frequently bundled track doesn't dominate labels.
    Before the first rebuild, use the library's artist genres."""
    bundles = store.get_proposals("mode_bundles") or {}
    tracks = {}
    for mid, bucket in bundles.items():
        if mid.startswith("_"):
            continue
        for items in bucket.values():
            for item in items:
                if item.get("key"):
                    tracks.setdefault(item["key"], item)
    items = attach_genres(store, [copy(item) for item in tracks.values()])
    by_artist = store.artist_genre_years()
    if not items:
        items = list(by_artist.values())
    families = defaultdict(Counter)
    for item in items:
        genre = (item.get("genre") or (by_artist.get(item.get("artist")) or {}).get("genre") or "").strip().lower()
        if genre:
            families[genre_map.family(genre)][genre] += 1
    plays = {g.lower(): count for g, count in RecDao(store).genre_play_distribution().items()}
    family_plays = Counter()
    for genre, count in plays.items():
        family_plays[genre_map.family(genre)] += count
    # Put the user's most-listened-to families first, with their familiar subgenre as the label.
    # With no listening history, candidate depth is more useful than the taxonomy's alphabet.
    options = [{"family": family,
                "label": min(counts, key=lambda g: (-plays.get(g, 0), -counts[g], g))}
               for family, counts in families.items()]
    return sorted(options, key=lambda o: (-family_plays[o["family"]],
                                         -sum(families[o["family"]].values()), o["label"]))
