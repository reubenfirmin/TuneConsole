"""Production specialist registry; each purpose has an independent artifact."""
from yt_playlist.rec import (audio_vibe_cruise, audio_vibe_release, audio_vibe_relax,
                             audio_vibe_uplift, audio_vibe_energize, audio_vibe_focus)

SPECIALISTS = (
    ('cruise', audio_vibe_cruise), ('release', audio_vibe_release), ('relax', audio_vibe_relax),
    ('uplift', audio_vibe_uplift), ('energize', audio_vibe_energize), ('concentrate', audio_vibe_focus),
)


def identity():
    return '/'.join(module.identity() for _vibe, module in SPECIALISTS)
