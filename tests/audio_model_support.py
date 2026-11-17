"""Explicit model selection for private test preparation."""
from yt_playlist.rec.audio_vibe_specialists import SPECIALISTS


def disable_models(patch):
    for _vibe, module in SPECIALISTS:
        patch.setenv(module.ENVIRONMENT_KEY, '')


def enable_models(patch):
    for _vibe, module in SPECIALISTS:
        patch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
        patch.setenv(module.MODE_KEY, 'active')
