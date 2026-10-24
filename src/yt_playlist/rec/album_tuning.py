"""Temporary album-menu controls over the worker's model-ranked frontier."""
import json
import math

from yt_playlist.rec import rec_params

SETTING = 'album_menu_tuning'
DURATION_S = 4 * 3600
AXES = (
    {'key': 'novelty', 'low': 'Comfort', 'high': 'Novelty', 'color': 'var(--radio-queue-2)'},
)


def defaults(store):
    return {axis['key']: rec_params.get_param(store, 'album_menu_' + axis['key']) for axis in AXES}


def state(store, now):
    values = defaults(store)
    try:
        saved = json.loads(store.settings.get_setting(SETTING) or '{}')
        remaining = max(0, min(DURATION_S, saved.get('until', 0) - now))
        overrides = saved.get('values', {})
        # Keep active genre choices while retiring the old overlapping sliders.
        if set(overrides) == {'discovery', 'stretch', 'variety', 'recency'}:
            overrides = {'novelty': overrides['discovery']}
        genres = saved.get('genres', [])
        if (remaining and math.isfinite(saved['until']) and isinstance(genres, list)
                and len(genres) <= 100 and all(isinstance(g, str) and 0 < len(g) <= 100 for g in genres)
                and set(overrides) == set(values) and all(
                type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
                for value in overrides.values())):
            return {'values': overrides, 'genres': genres, 'remaining': remaining}
    except (ValueError, TypeError, AttributeError):
        pass
    return {'values': values, 'genres': [], 'remaining': 0}


def apply(store, now, values, genres):
    if set(values) != {axis['key'] for axis in AXES} or any(
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
            for value in values.values()):
        raise ValueError('Invalid album controls')
    if len(genres) > 100 or any(not isinstance(genre, str) or not genre or len(genre) > 100 for genre in genres):
        raise ValueError('Invalid album genres')
    store.settings.set_setting(SETTING, json.dumps({'until': now + DURATION_S,
        'values': values, 'genres': sorted(set(genres))}))


def reset(store):
    store.settings.delete_setting(SETTING)
