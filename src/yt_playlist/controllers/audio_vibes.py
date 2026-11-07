"""Audio collection policy over immutable evidence and SQL receipts."""
from threading import RLock
import json

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core import audio_annotation_contract as annotations
from yt_playlist.core import audio_vibe_metadata


class AudioVibes:
    def __init__(self, repository, samples, annotation_files, prepared_content, metadata_files):
        self.repository, self.sample_files = repository, samples
        self.annotation_files = annotation_files
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
        for reference in self.repository.sample_facts(run_id):
            try:
                result.append(self.sample_files.read(reference))
            except (OSError, ValueError):
                continue
        return result

    def save_sample(self, sample, now):
        with self._lock:
            self._require_video(self.playback(sample['run_id']), sample['video_id'])
            existing = self.repository.sample_conflicts(sample['run_id'], sample['id'])
            for previous in existing:
                if previous['run_id'] == sample['run_id']:
                    self._require_video(previous, sample['video_id'])
                if previous['point'] == sample['point'] or previous['id'] == sample['id']:
                    if (any(previous[name] != sample[name] for name in ('id', 'run_id', 'point', 'video_id'))
                            or not self.sample_files.matches(previous['filename'], sample)):
                        raise ValueError('This sampling point has already been saved')
                    self.sample_files.repair(previous['filename'], sample)
                    # Requeue on an identical retry too: a prior acknowledgement
                    # may have failed after repairing the file but before enqueue.
                    self.prepared_content.requeue_recording(previous['recording_id'])
                    return
            filename = self.sample_files.write(sample)
            self.repository.save_sample(sample, filename, now, expected=existing)

    def feedback(self, run_id):
        row = self.repository.annotation(run_id, kind='session', taxonomy_version=TAXONOMY_VERSION)
        return self._feedback(row) if row else None

    def _feedback(self, row):
        document = self.annotation_files.read(row)
        legacy = (document.get('legacy') or {}).get('sample_ids')
        samples = json.loads(legacy) if legacy is not None else row['sample_ids']
        return dict(taxonomy_version=row['taxonomy_version'], vibes=row['vibes'],
                    prediction=document['prediction'], updated_at=row['created_at'], sample_ids=samples)

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
        sample_ids, vibes, _state = annotations.validate(run_id, sample_ids, vibes, now)
        with self._lock:
            if self.repository.playback(run_id) is None:
                raise ValueError('A new annotation requires an existing playback run')
            known, vocabulary = self.repository.taxonomy(taxonomy_version)
            if not known or not set(vibes) <= vocabulary:
                raise ValueError('Labels do not belong to the annotation taxonomy')
            owners = self.repository.sample_owners(sample_ids)
            if any(owners.get(sample) != run_id for sample in sample_ids):
                raise ValueError('Annotation sample is missing or belongs to another run')
            previous = self.repository.annotation(run_id, kind='session', taxonomy_version=taxonomy_version)
            state = ('labeled' if vibes else 'withdrawn'
                     if previous and previous['state'] in ('labeled', 'withdrawn') else 'empty')
            if previous and previous['state'] == state:
                document = self.annotation_files.read(previous)
                if (previous['sample_ids'] == sample_ids and previous['vibes'] == vibes
                        and document['prediction'] == prediction):
                    return
            revision = previous['revision'] + 1 if previous else 1
            prediction_version = prediction.get('taxonomy_version') if isinstance(prediction, dict) else None
            _known, predicted_vocabulary = self.repository.taxonomy(prediction_version)
            evaluation = annotations.evaluation_values(prediction, predicted_vocabulary)
            document = dict(format=annotations.FILE_FORMAT, run_key=run_id, kind='session',
                            taxonomy_version=taxonomy_version, revision=revision,
                            prediction=prediction, unresolved_samples=[])
            with self.annotation_files.write(document) as reference:
                self.repository.publish_annotation(run_id, sample_ids, vibes, taxonomy_version,
                    now, state=state, revision=revision, reference=reference, evaluation=evaluation,
                    expected=previous['id'] if previous else None)

    def resolve_annotations(self, run_id):
        return self.repository.resolve_annotations(run_id)
