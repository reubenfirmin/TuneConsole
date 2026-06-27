"""Discovery-pool bounding and rotation policy (#52).

The album discovery pool must stay small and keep moving, instead of accumulating every album of every
artist you have ever touched. Two ideas, both pure (no IO, no network) so they are trivially testable:

- Only the top interest-ranked artists are scanned at all (the caller restricts the universe).
- Each scanned artist holds only a few albums in the pool at once, but those ROTATE across the artist's
  whole discography over time. Fills are drawn uniformly at random across the entire catalog, so a great
  old album is exactly as likely to surface as the newest release; an album that has been shown to you
  rotates out to make room for one you have not seen.

The repo layer applies these decisions (delete/insert); this module only decides.
"""

# Defaults mirror the rec_params knobs discovery_artist_limit / discovery_albums_per_artist; the caller
# passes the live values, these are the fallbacks.
DEFAULT_ARTIST_LIMIT = 100
DEFAULT_ALBUMS_PER_ARTIST = 3


def choose_album_keep(pooled, n, rng):
    """From an artist's currently pooled albums ({browse_id: offered_count}), choose up to `n` to RETAIN
    with no network call (the cleanup / per-pass guard path). Unshown albums (offered_count == 0) are
    preferred over shown ones; ties are broken randomly so the retained sample spreads across the
    catalog (old and new) rather than fixing on whatever sorts first. Returns the retained browse_id set;
    the caller deletes the rest."""
    ids = list(pooled)
    rng.shuffle(ids)                                  # random tie-break -> varied retain, not newest-biased
    ids.sort(key=lambda b: (pooled.get(b, 0) > 0))    # stable: unshown (False) first, shown (True) last
    return set(ids[:n])


def rotate_album_sample(catalog_albums, pooled, n, rng):
    """Decide an artist's pool sample after a fresh scan. `catalog_albums` is the artist's full unowned
    discography ([{browse_id, title, year, thumbnail}, ...]); `pooled` is {browse_id: offered_count} for
    what that artist currently has in the pool. Returns (keep_ids, add_albums):

    - keep_ids: pooled albums to RETAIN (unshown ones still in the catalog, capped at n).
    - add_albums: new album dicts to INSERT, drawn uniformly at random across the WHOLE catalog (so old
      albums rotate in, not just the newest), filling the remaining slots.

    The caller deletes every currently-pooled album not in keep_ids (shown albums and overflow rotate
    out), then inserts add_albums.
    """
    cat_ids = {a["browse_id"] for a in catalog_albums}
    unshown_pooled = {b: 0 for b, oc in pooled.items() if oc == 0 and b in cat_ids}
    keep = choose_album_keep(unshown_pooled, n, rng)
    need = n - len(keep)
    add = []
    if need > 0:
        fresh = [a for a in catalog_albums if a["browse_id"] not in pooled]
        rng.shuffle(fresh)                            # uniform across the catalog: old gems get a turn
        add = fresh[:need]
    return keep, add
