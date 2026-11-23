"""Audio collection policy over immutable evidence and SQL receipts."""
from threading import RLock
import json

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core import audio_annotation_contract as annotations
from yt_playlist.core import audio_vibe_metadata


class AudioVibes:
    def __init__(self, repository, samples, prepared_content, metadata_files):
        self.repository, self.sample_files = repository, samples
        self.prepared_content = prepared_content
        self.metadata_files = metadata_files
        self._lock = RLock()

    def base_metadata(self, video_id):
        with self.metadata_files.retained():
            inputs = self.repository.base_metadata_inputs(video_id)
            return audio_vibe_metadata.assemble(inputs,
                (self.metadata_files.source_values(reference) for reference in inputs['sources']))

    def playback(self, run_id):
        row = self.repository.playback(run_id)
        if row is not None:
            return row
        samples = self.samples(run_id)
        return {key: samples[0][key] for key in ('video_id', 'title', 'artist')} if samples else None

    @staticmethod
    def _require_video(previous, video_id):
        if previous and previous['video_id'] != video_id:
            raise ValueError('The playback run belongs to a different song')

    def save_playback(self, run_id, video_id, title, artist, now):
        with self._lock:
            self._require_video(self.playback(run_id), video_id)
            self.repository.save_playback(run_id, video_id, title, artist, now)

    def sample_receipts(self, run_id):
        return self.repository.sample_receipts(run_id)

    def samples(self, run_id):
        result = []
        with self.sample_files.retained():
            for reference in self.repository.sample_facts(run_id):
                try:
                    result.append(self.sample_files.read(reference))
                except (OSError, ValueError):
                    continue
        return result

    def save_sample(self, sample, now):
        with self._lock, self.sample_files.retained():
            self._require_video(self.playback(sample['run_id']), sample['video_id'])
            existing = self.repository.sample_conflicts(sample['run_id'], sample['id'])
            for previous in existing:
                if previous['run_id'] == sample['run_id']:
                    self._require_video(previous, sample['video_id'])
                if previous['point'] == sample['point'] or previous['id'] == sample['id']:
                    if (any(previous[name] != sample[name] for name in ('id', 'run_id', 'point', 'video_id'))
                            or not self.sample_files.matches_reference(previous, sample)):
                        raise ValueError('This sampling point has already been saved')
                    self.sample_files.publish_reserved(previous, sample)
                    self.repository.complete_sample(sample['id'], previous)
                    # Requeue on an identical retry too: a prior acknowledgement
                    # may have failed after repairing the file but before enqueue.
                    self.prepared_content.requeue_recording(previous['recording_id'])
                    return
            reference = self.sample_files.reference(sample)
            self.repository.reserve_sample(sample, reference, now, expected=existing)
            self.sample_files.publish_reserved(reference, sample)
            self.repository.complete_sample(sample['id'], reference)

    def feedback(self, run_id):
        row = self.repository.annotation(run_id, kind='session', taxonomy_version=TAXONOMY_VERSION)
        return self._feedback(row) if row else None

    @staticmethod
    def _feedback(row):
        document = row['archive']
        legacy = (document.get('legacy') or {}).get('sample_ids')
        samples = json.loads(legacy) if legacy is not None else row['sample_ids']
        return dict(taxonomy_version=row['taxonomy_version'], vibes=row['vibes'],
                    prediction=document.get('prediction'), updated_at=row['created_at'], sample_ids=samples)

    def latest_feedback(self, video_id, *, exclude_run_id):
        run = self.repository.latest_feedback_run(video_id, exclude_run_id=exclude_run_id,
                                                  taxonomy_version=TAXONOMY_VERSION)
        return {'run_id': run, **self.feedback(run)} if run else None

    def legacy_feedback(self, run_id):
        row = self.repository.annotation(run_id, kind='feedback',
                                         taxonomy_version=annotations.LEGACY_FEEDBACK_TAXONOMY)
        if row is None:
            return None
        value = self._feedback(row)
        return {'vibe': value['vibes'][0] if value['vibes'] else '',
                **{key: value[key] for key in ('sample_ids', 'prediction', 'updated_at')}}

    def earlier_feedback(self, run_id):
        result = [self._feedback(row) for row in self.repository.annotation_heads(run_id)
                  if row['kind'] == 'development' or
                  (row['kind'] == 'session' and row['taxonomy_version'] != TAXONOMY_VERSION)]
        legacy = self.legacy_feedback(run_id)
        if legacy:
            result.append(legacy)
        return result

    def save_feedback(self, run_id, sample_ids, vibes, taxonomy_version, prediction, now):
        self.repository.save_annotation(run_id, sample_ids, vibes, taxonomy_version, prediction, now)
