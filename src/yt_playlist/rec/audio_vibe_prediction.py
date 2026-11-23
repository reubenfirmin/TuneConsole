"""Public audio prediction entrypoint for annotation and recording preparation."""
from yt_playlist.controllers.vibe_prediction import configured
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION as TAXONOMY_VERSION
from yt_playlist.rec.audio_vibe_values import (
    ATTRIBUTE_LABELS as ATTRIBUTE_LABELS,
    LABELS as LABELS,
    VERSION as VERSION,
)


def predict(samples, *, metadata=None, require_complete=True):
    """Run independent specialists against compatible excerpt embeddings."""
    return configured().capture().predict(samples, metadata=metadata, require_complete=require_complete)
