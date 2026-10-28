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
            'vibes': profile.get('vibes', {}), 'reliability': 'uncalibrated'}


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


def _intensity_fit(item):
    """Select the same usable learned input for scoring and served provenance."""
    fits = ((item.get('musical') or {}).get('vibes') or {}).get('fits') or {}
    if not isinstance(fits, dict):
        return None
    available = [(name, fit) for name in ('energize', 'release') if isinstance(fit := fits.get(name), dict)
                 and fit.get('source') != 'heuristic' and type(fit.get('score')) in (int, float)
                 and math.isfinite(fit['score']) and 0 <= fit['score'] <= 1]
    return max(available, key=lambda row: row[1]['score']) if available else None


def intensity_evidence(item):
    """Momentum or force can make a song intense, including slow heavy music."""
    chosen = _intensity_fit(item)
    if chosen is not None:
        _name, fit = chosen
        return fit['score'], fit.get('support', 0)
    return dimension(item, 'intensity')


def intensity(item):
    value, _support = intensity_evidence(item)
    return .5 if value is None else value


def provenance(item):
    """Freeze the actual input, keeping missing evidence distinct from neutral scoring."""
    profile = item.get('musical') or {}
    chosen = _intensity_fit(item)
    if chosen is None:
        value, support = dimension(item, 'intensity')
        name, source = None, None
    else:
        name, fit = chosen
        value, support, source = fit['score'], fit.get('support', 0), fit.get('source')
    return {'musical_policy': VERSION, 'musical_version': profile.get('version'),
            'musical_digest': profile.get('digest'), 'intensity': value, 'intensity_support': support,
            'intensity_source': source, 'intensity_vibe': name}
