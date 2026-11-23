"""Historical shared-model replay; never imported by application code."""
from yt_playlist.rec.audio_vibe_prediction import ATTRIBUTE_LABELS, LABELS, TAXONOMY_VERSION
from experiments.audio_lab_tools.legacy import audio_vibe_heuristic, audio_vibe_model
from experiments.audio_lab_tools.legacy.audio_vibe_heuristic import MATCH_THRESHOLD, VERSION


def predict(samples, *, metadata=None, require_complete=True, use_model=True):
    result = audio_vibe_heuristic.heuristic(samples, metadata=metadata, require_complete=require_complete)
    return audio_vibe_model.apply(samples, result, metadata) if use_model and result is not None else result
