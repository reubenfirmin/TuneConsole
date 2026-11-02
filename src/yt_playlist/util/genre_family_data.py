"""Static genre family data shared by the catalog and current-family adapters."""
import functools
import json
from pathlib import Path

from yt_playlist.util import genre_catalog
from yt_playlist.core import fao

_DATA = Path(__file__).parent.parent / 'data' / 'genre_families.json'


@functools.lru_cache(maxsize=1)
def load():
    raw = json.loads(fao.read_bytes(_DATA))
    families = raw['families']
    adjacency = raw['adjacency']
    energy = raw.get('energy', {})
    genre_to_family = {}
    for fam, genres in families.items():
        for genre in genres:
            genre_to_family[genre_catalog.normalize(genre)] = fam
    records, labels = genre_catalog.catalog()
    for label, (key, _, _) in labels.items():
        if fam := records[key]['family']:
            genre_to_family[label] = fam
    families = {fam: sorted(set(members) | {
        record['name'].lower() for record in records.values() if record['family'] == fam
    }) for fam, members in families.items()}
    return genre_to_family, adjacency, energy, families


def family(genre):
    if not genre:
        return ''
    genre_to_family, _, _, _ = load()
    return genre_to_family.get(genre_catalog.normalize(genre), f'other:{genre.strip().lower()}')
