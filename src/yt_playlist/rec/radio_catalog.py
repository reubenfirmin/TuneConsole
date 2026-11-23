"""Bounded catalog hydration for continuing On Air streams.

Membership is prepared by the existing recommendation worker. Legacy sessions
without that index retain the #286 genre fallback until their next launch.
"""
from yt_playlist.rec.radio_genres import GenrePolicy
from yt_playlist.rec.radio_ranking import eligible_page


def fallback_pool(store, allowed, *, limit):
    """Fill a bounded cold pool from eligible library rows, past rejected pages."""
    from yt_playlist.controllers.radio_inputs import RadioCatalog
    return RadioCatalog(store.radio_catalog).fallback(allowed, limit=limit)


def cluster_pool(store, keys, excluded, excluded_keys, allowed, *, limit):
    """Hydrate eligible recordings, scanning past stale/removed uploads in pages.

    Each SQL read and admitted vector batch is bounded by limit. No ranking,
    fitting, or full-library vector read occurs on this path.
    """
    from yt_playlist.controllers.radio_inputs import RadioCatalog
    return RadioCatalog(store.radio_catalog).cluster(keys, excluded, excluded_keys, allowed, limit=limit)


def themed_pool(store, genres: GenrePolicy, excluded, excluded_keys, allowed, *, limit):
    """Read another eligible genre batch, including aliases and upload overrides."""
    from yt_playlist.controllers.radio_inputs import RadioCatalog
    return RadioCatalog(store.radio_catalog).themed(genres, excluded, excluded_keys, allowed, limit=limit)
