"""Compatibility shared by Vibe training, score retention and serving."""
from fnmatch import fnmatchcase

from yt_playlist.core.audio_vibe_contract import ACOUSTICBRAINZ_ESTIMATE, POOLED_SOURCES

from . import vibe_score_versions
from .vibe_score_versions import compatible_version, serving_prefix

_GLOB = str.maketrans({'[': '[[]', '*': '[*]', '?': '[?]'})


def serving_pattern():
    # Quote SQLite GLOB metacharacters in the exact prefix and require a suffix.
    return serving_prefix().translate(_GLOB) + '?*'


def joint_suffix(configuration):
    from yt_playlist.rec import vibe_joint_model

    return '/'.join((vibe_joint_model.VERSION, configuration, vibe_score_versions.joint_prediction_policy()))


def score_patterns():
    from yt_playlist.rec import acousticbrainz_vibes, vibe_joint_state

    measurement = serving_pattern()
    joint = (measurement + '/' + joint_suffix(vibe_joint_state.config_identity()).translate(_GLOB)
             if vibe_joint_state.qualified() else None)
    estimate = acousticbrainz_vibes.estimate_prefix().translate(_GLOB) + '?*'
    return measurement, joint, estimate


def compatible_score(stamp, source, patterns):
    measurement, joint, estimate = patterns
    pattern = (joint if source in POOLED_SOURCES else
               {ACOUSTICBRAINZ_ESTIMATE: estimate, 'audio_measurement': measurement}.get(source))
    return bool(pattern and isinstance(stamp, str) and fnmatchcase(stamp, pattern))


def compatible_profile_vibes(vibes, *, patterns=None):
    stamp = vibes.get('inputs_version')
    if not compatible_version(stamp):
        return {}
    result = dict(vibes)
    if patterns is None:
        patterns = score_patterns()
    for field in ('fits', 'intensity_fits'):
        fits = vibes.get(field)
        if isinstance(fits, dict):
            result[field] = {name: fit for name, fit in fits.items() if isinstance(fit, dict)
                             and compatible_score(fit.get('inputs_version', stamp), fit.get('source'), patterns)}
        elif field in result:
            result.pop(field)
    return result
