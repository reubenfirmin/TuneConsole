"""Historical shared-model replay; never imported by application code."""
from yt_playlist.rec.audio_vibe_prediction import *
from yt_playlist.rec import audio_vibe_prediction as heuristic
from experiments.audio_lab_tools.legacy import audio_vibe_model


def predict(samples, *, metadata=None, require_complete=True, use_model=True):
    result = heuristic.predict(samples, metadata=metadata, require_complete=require_complete, use_model=False)
    return audio_vibe_model.apply(samples, result, metadata) if use_model and result is not None else result
