"""Explicit genre requests for the current Home menu, separate from learned preferences."""
from copy import copy
from collections import Counter, defaultdict

from yt_playlist.rec.ordering import _field, attach_genres
from yt_playlist.util import genre_map


def other_genres(store, items, avoid):
    return matching_genres(store, items, avoid=avoid)


def matching_genres(store, items, *, include=(), avoid=()):
    """Keep tracks with a known genre outside the offered families. Resolve current library
    metadata as rendering does, then use the description's artist fallback for untagged
    discoveries. Unknown metadata cannot establish a new genre."""
    if not include and not avoid:
        return items
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return matching_genres(reader, items, include=include, avoid=avoid)
    # Bundles can predate enrichment. Rendering gives library genres precedence, so
    # filter those same genres on copies instead of changing the cached candidates.
    items = resolved_genres(store, items)
    return [item for item in items if _field(item, "genre")
            and genre_map.family(_field(item, "genre"),conn=store.conn) not in avoid
            and (not include or genre_map.family(_field(item, "genre"),conn=store.conn) in include)]


def resolved_genres(store, items):
    """Copy candidates and attach the same current/fallback genres used in descriptions."""
    items = attach_genres(store, [copy(item) for item in items])
    by_artist = None
    for item in items:
        genre = (_field(item, "genre") or "").strip()
        if not genre:
            if by_artist is None:
                by_artist = store.rec_query.artist_genre_years_for(
                    [_field(candidate, 'artist') or '' for candidate in items if not _field(candidate, 'genre')])
            genre = (by_artist.get(_field(item, "artist") or "") or {}).get("genre") or ""
            if isinstance(item, dict):
                item["genre"] = genre
            else:
                item.genre = genre
    return items


def genre_options(store, *, genre_plays=None, bundles=None):
    """Offer genre families present in prepared candidates, using current track metadata.
    Deduplicate tracks across modes/lanes so a frequently bundled track doesn't dominate labels.
    Before the first rebuild, use the library's artist genres."""
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return genre_options(reader, genre_plays=genre_plays, bundles=bundles)
    if bundles is None and getattr(store, '_recommendation_files', False):
        return _stored_genre_options(store, genre_plays)
    if bundles is None:
        bundles = store.rec_proposals.get_proposals("mode_bundles") or {}
    tracks = {}
    for mid, bucket in bundles.items():
        if mid.startswith("_"):
            continue
        for items in bucket.values():
            for item in items:
                if item.get("key"):
                    tracks.setdefault(item["key"], item)
    items = attach_genres(store, [copy(item) for item in tracks.values()])
    by_artist = (store.rec_query.artist_genre_years_for([item.get('artist', '') for item in items if not item.get('genre')])
                 if items else store.rec_query.artist_genre_years())
    if not items:
        items = list(by_artist.values())
    families = defaultdict(Counter)
    for item in items:
        genre = (item.get("genre") or (by_artist.get(item.get("artist")) or {}).get("genre") or "").strip().lower()
        if genre:
            families[genre_map.family(genre,conn=store.conn)][genre] += 1
    return _genre_options(store, families, genre_plays)


def _genre_options(store, families, genre_plays):
    dist = store.rec_query.genre_play_distribution() if genre_plays is None else genre_plays
    plays = {g.lower(): count for g, count in dist.items()}
    family_plays = Counter()
    for genre, count in plays.items():
        family_plays[genre_map.family(genre,conn=store.conn)] += count
    # Put the user's most-listened-to families first, with their familiar subgenre as the label.
    # With no listening history, candidate depth is more useful than the taxonomy's alphabet.
    options = [{"family": family,
                "label": min(counts, key=lambda g: (-plays.get(g, 0), -counts[g], g))}
               for family, counts in families.items()]
    return sorted(options, key=lambda o: (-family_plays[o["family"]],
                                         -sum(families[o["family"]].values()), o["label"]))


def _stored_genre_options(store, genre_plays):
    """Count complete membership with bounded metadata reads and no bundle decode."""
    from yt_playlist.core.activity import check_cancellable_work
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return _stored_genre_options(reader, genre_plays)
    head = store.recommendations.head('home')
    families = defaultdict(Counter)
    counts = Counter()
    if head is not None:
        after = 0
        while True:
            check_cancellable_work()
            page = store.recommendations.recording_metadata_page('home', generation=head['id'], after=after)
            for item in resolved_genres(store, page['tracks']):
                genre = (item.get('genre') or '').strip().lower()
                if genre:
                    counts[genre] += 1
            after = page['after']
            if page['exhausted']:
                break
    if not counts:
        # The original fresh-user fallback remains useful before preparation.
        for item in store.rec_query.artist_genre_years().values():
            genre = (item.get('genre') or '').strip().lower()
            if genre:
                counts[genre] += 1
    # Resolve identities once per distinct label. A large account can repeat
    # the same genre in thousands of candidates and exhaust every worker slice.
    for genre, count in counts.items():
        families[genre_map.family(genre,conn=store.conn)][genre] = count
    return _genre_options(store, families, genre_plays)
