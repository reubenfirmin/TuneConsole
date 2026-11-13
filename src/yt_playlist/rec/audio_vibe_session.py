"""Audio-vibe run state: playback/sample/feedback facts joined with a prediction."""
from yt_playlist.core.features import audio_vibes_enabled
from yt_playlist.rec import audio_vibe_prediction


def state(store, run_id, video_id=None, *, choices=None):
    playback = store.audio_vibes.playback(run_id)
    if not audio_vibes_enabled():
        # Collection needs acknowledgements, not the developer's annotation
        # history or full diagnostic payloads. Preparation predicts off-thread.
        return {'run_id': run_id, 'playback': playback,
                'samples': store.audio_vibes.sample_receipts(run_id),
                'taxonomy_version': audio_vibe_prediction.TAXONOMY_VERSION}
    samples = store.audio_vibes.samples(run_id)
    metadata = store.audio_vibes.base_metadata(samples[0]['video_id']) if samples else None
    video_id = playback['video_id'] if playback else video_id
    feedback = store.audio_vibes.feedback(run_id)
    previous = (store.audio_vibes.latest_feedback(video_id, exclude_run_id=run_id)
                if video_id and feedback is None else None)
    return {'run_id': run_id, 'playback': playback,
            'samples': samples, 'choices': choices,
            'taxonomy_version': audio_vibe_prediction.TAXONOMY_VERSION,
            'prediction': audio_vibe_prediction.predict(samples, metadata=metadata),
            'feedback': feedback, 'previous_feedback': previous,
            'legacy_feedback': store.audio_vibes.earlier_feedback(run_id)}
