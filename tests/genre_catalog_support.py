"""Bound vocabulary inputs when a test owns SQL rather than catalog curation."""
from yt_playlist.util import genre_catalog


def bound_catalog(patch, *, entries=(('rock', 'Rock'), ('jazz', 'Jazz'))):
    records = {key: {'key': key, 'name': name, 'mbid': None, 'source': 'curated',
                     'family': key, 'family_source': 'curated'}
               for key, name in entries}
    labels = {key: (key, row['name'], 'curated') for key, row in records.items()}
    patch.setattr(genre_catalog, 'catalog', lambda: (records, labels))
    patch.setattr(genre_catalog, 'family_labels', lambda: dict(entries))
    patch.setattr(genre_catalog, 'builtin_names', lambda: [name for _key, name in entries])
    patch.setattr(genre_catalog, 'legacy_names', lambda: [name for _key, name in entries])
    patch.setattr(genre_catalog, 'legacy_entries', lambda: [(name, []) for _key, name in entries])
