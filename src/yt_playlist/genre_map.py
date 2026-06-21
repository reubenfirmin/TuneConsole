"""Meta-genre map (spec §2.1): genre -> family + family-to-family distance.

Loaded from the hand-editable data/genre_families.json. Used to build genre-FAMILY baskets
for the embedding content blend and to measure genre diversity/adjacency. A genre not in the
map falls into its own singleton family (distance 1 to everything else).
"""
import functools
import json
from pathlib import Path

_DATA = Path(__file__).parent / "data" / "genre_families.json"


@functools.lru_cache(maxsize=1)
def _load():
    raw = json.loads(_DATA.read_text())
    families = raw["families"]
    adjacency = raw["adjacency"]
    genre_to_family = {}
    for fam, genres in families.items():
        for g in genres:
            genre_to_family[g.lower()] = fam
    return genre_to_family, adjacency


def family(genre) -> str:
    """Family for a genre (case-insensitive). Unknown genres map to 'other:<genre>' (singleton)."""
    if not genre:
        return ""
    g = genre.strip().lower()
    g2f, _ = _load()
    return g2f.get(g, f"other:{g}")


def family_distance(fam_a, fam_b) -> float:
    """Distance in [0, 1] between two families: 0 same, listed adjacency value, else 1."""
    if fam_a == fam_b:
        return 0.0
    _, adj = _load()
    near = adj.get(fam_a, {}).get(fam_b)
    if near is None:
        near = adj.get(fam_b, {}).get(fam_a)
    return near if near is not None else 1.0


def distance(genre_a, genre_b) -> float:
    """Distance in [0, 1] between two genres via their families."""
    return family_distance(family(genre_a), family(genre_b))
