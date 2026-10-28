"""Save internal development annotations and freeze the hypothesis they evaluate.

These popup answers train/evaluate the vibe model; they do not set product
preferences or override a track's vibe in recommendation ranking. See the
usage boundary in core.audio_vibe_contract.
"""

from yt_playlist.rec import audio_vibe_preview


def save(store, run_id, sample_ids, vibes, taxonomy_version, now):
    if (taxonomy_version != audio_vibe_preview.TAXONOMY_VERSION
            or len(set(vibes)) != len(vibes)
            or any(vibe not in audio_vibe_preview.LABELS for vibe in vibes)):
        raise ValueError('Feedback must use the current vibe targets')
    # The previous hypothesis and its replacement belong to one publication.
    with store._lock:
        samples = {sample['id']: sample for sample in store.audio_vibes.samples(run_id)}
        if (not store.audio_vibes.playback(run_id) or len(set(sample_ids)) != len(sample_ids)
                or any(sample_id not in samples for sample_id in sample_ids)):
            raise ValueError('Feedback must reference saved excerpts from this playback')
        previous = store.audio_vibes.feedback(run_id)
        selected = [samples[sample_id] for sample_id in sample_ids]
        metadata = store.audio_vibes.base_metadata(selected[0]['video_id']) if selected else None
        prediction = (previous['prediction'] if previous
                      and set(previous['sample_ids']) == set(sample_ids)
                      else audio_vibe_preview.predict(selected, metadata=metadata))
        store.audio_vibes.save_feedback(run_id, sample_ids, vibes, taxonomy_version, prediction, now)
