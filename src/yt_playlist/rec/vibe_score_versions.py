"""Version identifiers shared by Vibe scoring, training and serving."""
from yt_playlist.core.audio_vibe_contract import JOINT_AUDIO_POLICY, fit_family_prefix

JOINT_RELATION_POLICY = 'sampled-artist-cohort-1'


def serving_prefix():
    from . import audio_vibe_prediction, musical_profiles

    return fit_family_prefix() + musical_profiles.VERSION + '/' + audio_vibe_prediction.VERSION + '/'


def compatible_version(stamp, *, prefix=None):
    if prefix is None:
        prefix = serving_prefix()
    return isinstance(stamp, str) and stamp.startswith(prefix) and len(stamp) > len(prefix)


def joint_prediction_policy():
    return JOINT_AUDIO_POLICY + '/' + JOINT_RELATION_POLICY
