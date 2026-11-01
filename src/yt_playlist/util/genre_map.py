"""Meta-genre map (spec §2.1): genre -> family + family-to-family distance.

The hand-editable genre_families.json supplies curated anchors, distance and energy. The shared
catalog projects explicit MusicBrainz parent relationships onto those same two levels. Unmapped
or ambiguous genres keep a singleton family (distance 1 to everything else).
"""
from yt_playlist.util import genre_catalog, genre_family_data
from yt_playlist.util.genre_family_data import load as _load


def family(genre, *, conn=None) -> str:
    """Family for a genre (case-insensitive). Unknown genres map to 'other:<genre>' (singleton)."""
    if not genre:
        return ""
    if conn is not None:
        from yt_playlist.core import genre_identity
        return genre_identity.family(conn,genre)
    return genre_family_data.family(genre)


def family_distance(fam_a, fam_b) -> float:
    """Distance in [0, 1] between two families: 0 same, listed adjacency value, else 1."""
    if fam_a == fam_b:
        return 0.0
    _, adj, _, _ = _load()
    near = adj.get(fam_a, {}).get(fam_b)
    if near is None:
        near = adj.get(fam_b, {}).get(fam_a)
    return near if near is not None else 1.0


def distance(genre_a, genre_b) -> float:
    """Distance in [0, 1] between two genres via their families."""
    return family_distance(family(genre_a), family(genre_b))


def energy(genre) -> float:
    """Genre-family energy/intensity in [0,1] (0 = mellow, 1 = intense). Unknown/untagged -> 0.5,
    so an untagged track floats to the middle of every energy journey rather than sinking."""
    _, _, en, _ = _load()
    return en.get(family(genre), 0.5)


def subgenre(genre, *, conn=None) -> str | None:
    """The normalized (lowercased) subgenre tag for a known genre, or None if unknown.
    Distinct from family(): family() collapses subgenres to a bucket; subgenre() keeps the fine axis."""
    if not genre:
        return None
    if conn is not None:
        from yt_playlist.core import genre_identity
        key, _ = genre_identity.resolve(conn,genre)
        return key
    records, labels = genre_catalog.catalog()
    if label := labels.get(genre_catalog.normalize(genre)):
        return records[label[0]]["name"].lower()
    g2f, _, _, _ = _load()
    return genre.strip().lower() if genre_catalog.normalize(genre) in g2f else None


def subgenres_of(fam, *, conn=None) -> list:
    """All member genre tags (lowercased) for the given family. Empty list if the family is unknown."""
    if conn is not None:
        retired = ("AND NOT EXISTS(SELECT 1 FROM genre_redirects r WHERE r.retired_key=c.key)"
                   if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone() else '')
        return [row[0] for row in conn.execute('SELECT c.key FROM genre_catalog c WHERE family_key=? '
                                              +retired+' ORDER BY c.name COLLATE NOCASE',(fam,))]
    _, _, _, families = _load()
    members = families.get(fam)
    if members is None:
        return []
    return [g.lower() for g in members]


def all_genres() -> list:
    """Sorted list of all lowercased genre tags known to the map."""
    g2f, _, _, _ = _load()
    _, _, _, families = _load()
    return sorted({g.lower() for members in families.values() for g in members})


def all_families() -> list:
    """Sorted list of all lowercased family names known to the map."""
    _, _, _, families = _load()
    return sorted(f.lower() for f in families.keys())
