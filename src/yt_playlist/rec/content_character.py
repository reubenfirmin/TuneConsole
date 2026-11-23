"""Recording character admitted to the shared content encoders."""
from math import sqrt

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.core.audio_vibe_contract import POOLED_SOURCES
from yt_playlist.rec import musical_profiles, vibe_intensity
from yt_playlist.rec.vibe_score_policy import compatible_profile_vibes

VERSION = 'content-character-2/' + musical_profiles.VERSION


def _conflicted(dimension):
    return dimension.get('conflict') or any(_conflicted(item) for item in dimension.get('evidence', ()))


def _specialist(fit, *, learned):
    if not isinstance(fit, dict):
        return None
    source = fit.get('source')
    if not learned:
        return fit if source == 'audio_measurement' else None
    # The caller has checked the fit version. Its support already discounts
    # posterior uncertainty and coverage.
    if source in POOLED_SOURCES:
        support = musical_profiles.number(fit.get('support'))
        if support:
            return {**fit, 'support': .5 * support}
    return None


def evidence(profile):
    """Return supported track features and their independent recording subset.

    Both views have at most one observation per dimension. Artist aggregation
    only receives the independent subset, since estimates share conditioning
    with other recordings, including the artist being described.
    """
    if not profile or profile.get('version') != musical_profiles.VERSION:
        return {}, {}
    dimensions = profile['dimensions']
    original = {}
    for name in musical_profiles.DIMENSIONS:
        dimension = dimensions.get(name) or {}
        if not musical_profiles.independent(dimension):
            dimension = musical_profiles.combine([observation for observation in dimension.get('evidence', ())
                if musical_profiles.independent(observation) and observation.get('value') is not None])
        value = musical_profiles.number(dimension.get('value'))
        support = musical_profiles.number(dimension.get('support'))
        if value is not None and support:
            original[name] = (value, support * (.5 if _conflicted(dimension) else 1))

    intensity = dimensions.get('intensity') or {}
    fits = compatible_profile_vibes(profile.get('vibes') or {}).get('intensity_fits', {})
    if intensity.get('override'):
        return original, original
    measured = vibe_intensity.ensemble({name: _specialist(fit, learned=False) for name, fit in fits.items()})
    if measured and measured['support'] > original.get('intensity', (None, 0))[1]:
        original['intensity'] = (measured['score'], measured['support'])
    if 'intensity' not in original:
        estimate = vibe_intensity.ensemble({name: _specialist(fit, learned=True) for name, fit in fits.items()})
        if estimate:
            return {**original, 'intensity': (estimate['score'], estimate['support'])}, original
    return original, original


def load(store, catalogs):
    """Project retained profiles in bounded pages without keeping their full lineage in memory."""
    result = {}
    keys = sorted(catalogs)
    for offset in range(0, len(keys), 64):
        check_stopping()
        page = keys[offset:offset + 64]
        profiles = store.musical_profiles.by_keys(page)
        for key in page:
            result[key] = evidence(profiles.get(key))
    return result


def audio(catalog, character):
    return {**{field: catalog[field] for field in ('music_key', 'music_scale') if (catalog or {}).get(field)},
            'character': character}


def fingerprint(catalog_digest, character):
    return musical_profiles.digest([VERSION, catalog_digest, character])


# Each supported axis projects onto two endpoints. Missing axes have no
# coordinate, and a mean-valued observation still has positive support. Related
# axes share the energy budget of one channel.
COLUMNS = tuple((name, endpoint, .5 / sqrt(len(channel)))
                for channel in musical_profiles.CHANNELS for name in channel for endpoint in (0, 1))


def encode(columns, features, output):
    for index, (name, endpoint, weight) in enumerate(columns):
        if name in features:
            value, support = features[name]
            output[index] = weight * sqrt(support) * (value if endpoint else 1 - value)


def finish_vector(vector, policy):
    norm = float(np.linalg.norm(vector))
    if not norm:
        return None
    # Keep sparse character evidence below unit length so support still
    # matters when there is no catalog feature to anchor the normalization.
    divisor = max(1., norm) if policy == VERSION else norm
    return (vector / divisor).astype(np.float32)
