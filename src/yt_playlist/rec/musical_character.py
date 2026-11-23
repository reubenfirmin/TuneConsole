"""Small consumer views of prepared profiles, shared by radio and mixes.

No inference, provider request or candidate-pool normalization happens here.
The exact profile version and digest travel with each served recording.
"""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import musical_profiles, vibe_intensity
from yt_playlist.rec.vibe_score_policy import compatible_profile_vibes

VERSION = musical_profiles.VERSION


def compact(profile, *, provenance=True):
    if not profile or profile.get('schema') != 1 or profile.get('version') != VERSION:
        return None
    vibes = compatible_profile_vibes(profile.get('vibes') or {})
    if not provenance:
        return {'dimensions': profile['dimensions'], 'vibes': vibes}
    return {'version': profile['version'], 'digest': musical_profiles.digest(profile),
            'dimensions': {name: {key: value for key, value in dimension.items()
                                  if key in ('value', 'support', 'override', 'conflict')}
                           for name, dimension in profile['dimensions'].items()},
            'vibes': vibes, 'reliability': 'uncalibrated'}


def load(store, keys):
    keys = list(dict.fromkeys(keys))
    result = {}
    for offset in range(0, len(keys), 512):
        check_cancellable_work()
        result.update({key: compact(profile) for key, profile in
                       store.musical_profiles.by_keys(keys[offset:offset + 512]).items()})
    return result


def dimension(item, name):
    return musical_profiles.dimension(item.get('musical') or {}, name)


def _intensity_fit(item):
    """Select the same usable learned input for scoring and served provenance."""
    vibes = (item.get('musical') or {}).get('vibes') or {}
    fits = compatible_profile_vibes(vibes).get('intensity_fits') or {}
    combined = vibe_intensity.ensemble(fits)
    return (combined['selected'], combined) if combined else None


def intensity_evidence(item):
    """Momentum or force can make a song intense, including slow heavy music."""
    chosen = _intensity_fit(item)
    if chosen is not None:
        _name, fit = chosen
        return fit['score'], fit.get('support', 0)
    return None, 0


def intensity(item):
    value, _support = intensity_evidence(item)
    return .5 if value is None else value


def provenance(item):
    """Freeze the actual input, keeping missing evidence distinct from neutral scoring."""
    profile = item.get('musical') or {}
    chosen = _intensity_fit(item)
    if chosen is None:
        value, support, name, source = None, 0, None, None
    else:
        name, fit = chosen
        value, support, source = fit['score'], fit.get('support', 0), fit.get('source')
    return {'musical_policy': VERSION, 'musical_version': profile.get('version'),
            'musical_digest': profile.get('digest'), 'intensity': value, 'intensity_support': support,
            'intensity_source': source, 'intensity_vibe': name,
            'intensity_ensemble': chosen[1] if chosen is not None else None}
