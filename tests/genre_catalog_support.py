"""Bound vocabulary inputs when a test owns SQL rather than catalog curation."""
from functools import lru_cache

from yt_playlist.util import genre_catalog


# Captured during collection, before consumer fixtures replace these functions.
_SHIPPED_CATALOG = {name: getattr(genre_catalog, name) for name in (
    'catalog', 'family_labels', 'builtin_names', 'legacy_names', 'legacy_entries')}


def use_shipped_catalog(patch):
    """Build shared genuine history independently of a consumer's vocabulary."""
    for name, function in _SHIPPED_CATALOG.items():
        patch.setattr(genre_catalog, name, function)
    _isolate_catalog_caches(patch)


def _isolate_catalog_caches(patch):
    """Derived values must have the same lifetime as the patched vocabulary."""
    from yt_playlist.rec import musical_profiles
    from yt_playlist.util import genre_family_data, genre_map

    for module, name in ((genre_family_data, 'load'), (genre_catalog, '_parents'),
                         (genre_catalog, '_ancestors'), (musical_profiles, 'genre_profile')):
        cached = getattr(module, name)
        patch.setattr(module, name, lru_cache(**cached.cache_parameters())(cached.__wrapped__))
    patch.setattr(genre_map, '_load', genre_family_data.load)


def bound_shipped_catalog(patch, keys):
    """Retain actual aliases and families for the identities under test."""
    records, labels = genre_catalog.catalog()
    records = {key: records[key] for key in keys}
    labels = {key: value for key, value in labels.items() if value[0] in records}
    families = {key: value for key, value in genre_catalog.family_labels().items()
                if key in {row['family'] for row in records.values()}}
    names = [row['name'] for row in records.values()]
    patch.setattr(genre_catalog, 'catalog', lambda: (records, labels))
    patch.setattr(genre_catalog, 'family_labels', lambda: families)
    patch.setattr(genre_catalog, 'builtin_names', lambda: list(names))
    patch.setattr(genre_catalog, 'legacy_names', lambda: list(names))
    patch.setattr(genre_catalog, 'legacy_entries', lambda: [(name, []) for name in names])
    _isolate_catalog_caches(patch)


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
    _isolate_catalog_caches(patch)
