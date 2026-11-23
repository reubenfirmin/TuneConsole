"""Audio capture publication and annotation, independent of HTTP and storage format."""
from yt_playlist.rec import audio_vibe_prediction


def annotation_prediction(playback, samples, previous, sample_ids, vibes, version, predict, metadata):
    if (version != audio_vibe_prediction.TAXONOMY_VERSION
            or len(set(vibes)) != len(vibes)
            or any(vibe not in audio_vibe_prediction.LABELS for vibe in vibes)):
        raise ValueError('Feedback must use the current vibe targets')
    by_id = {sample['id']: sample for sample in samples}
    if (not playback or len(set(sample_ids)) != len(sample_ids)
            or any(sample_id not in by_id for sample_id in sample_ids)):
        raise ValueError('Feedback must reference saved excerpts from this playback')
    if previous and set(previous['sample_ids']) == set(sample_ids):
        return previous['prediction']
    return predict([by_id[sample_id] for sample_id in sample_ids], metadata=metadata, require_complete=False)


class AudioCapture:
    def __init__(self, captures, *, publication, enabled, now, choices=None,
                 predict=None):
        self.captures, self.publication = captures, publication
        self.enabled, self.now, self.choices = enabled, now, choices
        self.predict = predict or audio_vibe_prediction.predict

    def state(self, run_id, video_id=None):
        with self.publication:
            playback = self.captures.playback(run_id)
            if not self.enabled():
                return dict(run_id=run_id, playback=playback,
                    samples=self.captures.sample_receipts(run_id),
                    taxonomy_version=audio_vibe_prediction.TAXONOMY_VERSION)
            samples = self.captures.samples(run_id)
            metadata = self.captures.base_metadata(samples[0]['video_id']) if samples else None
            video_id = playback['video_id'] if playback else video_id
            feedback = self.captures.feedback(run_id)
            previous = self.captures.latest_feedback(video_id, exclude_run_id=run_id) if video_id and feedback is None else None
            legacy = self.captures.earlier_feedback(run_id)
        return dict(run_id=run_id, playback=playback, samples=samples, choices=self.choices,
            taxonomy_version=audio_vibe_prediction.TAXONOMY_VERSION,
            prediction=self.predict(samples, metadata=metadata, require_complete=False), feedback=feedback,
            previous_feedback=previous, legacy_feedback=legacy)

    def started(self, value):
        self.captures.save_playback(**value, now=self.now())
        return self.state(value['run_id'])

    def sample(self, value):
        self.captures.save_sample(value, self.now())
        return self.state(value['run_id'])

    def feedback(self, run_id, sample_ids, vibes, taxonomy_version):
        with self.publication:
            samples = self.captures.samples(run_id)
            selected = {sample['id']: sample for sample in samples}
            first = selected.get(sample_ids[0]) if sample_ids else None
            metadata = self.captures.base_metadata(first['video_id']) if first else None
            prediction = annotation_prediction(self.captures.playback(run_id), samples,
                self.captures.feedback(run_id), sample_ids, vibes, taxonomy_version, self.predict, metadata)
            self.captures.save_feedback(run_id, sample_ids, vibes, taxonomy_version, prediction, self.now())
        return self.state(run_id)
