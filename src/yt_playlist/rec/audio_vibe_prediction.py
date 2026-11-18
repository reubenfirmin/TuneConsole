"""Public audio prediction entrypoint for annotation and recording preparation."""
from yt_playlist.controllers.vibe_prediction import configured
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION as TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_values
from yt_playlist.rec.audio_vibe_values import (
    ATTRIBUTE_LABELS as ATTRIBUTE_LABELS,
    LABELS as LABELS,
    MATCH_THRESHOLD as MATCH_THRESHOLD,
    VERSION as VERSION,
    specialist_values as specialist_values,
)


def predict(samples, *, use_model=True, metadata=None, require_complete=True):
    """Run independent specialists, or the explicit offline heuristic comparison."""
    if use_model:
        return configured().capture().predict(samples, metadata=metadata, require_complete=require_complete)
    return audio_vibe_values.heuristic(samples, metadata=metadata, require_complete=require_complete)
