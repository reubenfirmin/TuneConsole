"""Small consumer views of prepared profiles, shared by radio and mixes.

No inference, provider request or candidate-pool normalization happens here.
The exact profile version and digest travel with each served recording.
"""
import math

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import musical_profiles

VERSION = musical_profiles.VERSION


def compact(profile):
    if not profile or profile.get('schema') != 1:
        return None
    return {'version': profile['version'], 'digest': musical_profiles.digest(profile),
            'dimensions': {name: {key: value for key, value in dimension.items()
                                  if key in ('value', 'support', 'override', 'conflict')}
                           for name, dimension in profile['dimensions'].items()},
            'reliability': 'uncalibrated'}


def load(store, keys):
    keys = list(dict.fromkeys(keys))
    result = {}
    for offset in range(0, len(keys), 512):
        check_cancellable_work()
        result.update({key: compact(profile) for key, profile in
                       store.musical_profiles.by_keys(keys[offset:offset + 512]).items()})
    return result


def dimension(item, name):
    profile = item.get('musical') or {}
    value = profile.get('dimensions', {}).get(name) or {}
    number, support = value.get('value'), value.get('support', 0)
    if (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1
            or type(support) not in (int, float) or not math.isfinite(support) or not 0 < support <= 1):
        return None, 0
    return number, support


def intensity(item):
    value, _support = dimension(item, 'intensity')
    return .5 if value is None else value


def provenance(item):
    """Freeze the actual input, keeping missing evidence distinct from neutral scoring."""
    profile = item.get('musical') or {}
    value, support = dimension(item, 'intensity')
    return {'musical_policy': VERSION, 'musical_version': profile.get('version'),
            'musical_digest': profile.get('digest'), 'intensity': value, 'intensity_support': support}
