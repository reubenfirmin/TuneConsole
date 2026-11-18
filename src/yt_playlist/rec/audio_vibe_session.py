"""Audio-vibe run state: playback/sample/feedback facts joined with a prediction."""
from yt_playlist.core.features import audio_vibes_enabled
from yt_playlist.rec import audio_vibe_prediction
from yt_playlist.controllers.audio_capture import AudioCapture


def state(store, run_id, video_id=None, *, choices=None):
    return AudioCapture(store.audio_vibes, publication=store._lock, enabled=audio_vibes_enabled,
                        now=lambda: None, choices=choices, predict=audio_vibe_prediction.predict).state(run_id, video_id)
