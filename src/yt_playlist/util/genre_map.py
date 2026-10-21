"""Meta-genre map (spec §2.1): genre -> family + family-to-family distance.

The hand-editable genre_families.json supplies curated anchors, distance and energy. The shared
catalog projects explicit MusicBrainz parent relationships onto those same two levels. Unmapped
or ambiguous genres keep a singleton family (distance 1 to everything else).
"""
import functools
import json
from pathlib import Path

from yt_playlist.util import genre_catalog
_DATA = Path(__file__).parent.parent / "data" / "genre_families.json"   # data/ lives at the package root


@functools.lru_cache(maxsize=1)
def _load():
    raw = json.loads(_DATA.read_text())
    families = raw["families"]
    adjacency = raw["adjacency"]
    energy = raw.get("energy", {})
    genre_to_family = {}
    for fam, genres in families.items():
        for g in genres:
            genre_to_family[genre_catalog.normalize(g)] = fam
    records, labels = genre_catalog.catalog()
    for label, (key, _, _) in labels.items():
        if fam := records[key]["family"]:
            genre_to_family[label] = fam
    families = {fam: sorted(set(members) | {
        r["name"].lower() for r in records.values() if r["family"] == fam
    }) for fam, members in families.items()}
    return genre_to_family, adjacency, energy, families


def family(genre) -> str:
    """Family for a genre (case-insensitive). Unknown genres map to 'other:<genre>' (singleton)."""
    if not genre:
        return ""
    g = genre.strip().lower()
    g2f, _, _, _ = _load()
    return g2f.get(genre_catalog.normalize(g), f"other:{g}")


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


def subgenre(genre) -> str | None:
    """The normalized (lowercased) subgenre tag for a known genre, or None if unknown.
    Distinct from family(): family() collapses subgenres to a bucket; subgenre() keeps the fine axis."""
    if not genre:
        return None
    records, labels = genre_catalog.catalog()
    if label := labels.get(genre_catalog.normalize(genre)):
        return records[label[0]]["name"].lower()
    g2f, _, _, _ = _load()
    return genre.strip().lower() if genre_catalog.normalize(genre) in g2f else None


def subgenres_of(fam) -> list:
    """All member genre tags (lowercased) for the given family. Empty list if the family is unknown."""
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
