"""Save internal development annotations and freeze the hypothesis they evaluate.

These popup answers train/evaluate the vibe model; they do not set product
preferences or override a track's vibe in recommendation ranking. See the
usage boundary in core.audio_vibe_contract.
"""

from yt_playlist.controllers.audio_capture import AudioCapture


def save(store, run_id, sample_ids, vibes, taxonomy_version, now):
    AudioCapture(store.audio_vibes, publication=store._lock, enabled=lambda: False,
                 now=lambda: now).feedback(run_id, sample_ids, vibes, taxonomy_version)
