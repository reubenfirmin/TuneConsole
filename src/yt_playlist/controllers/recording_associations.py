"""Coordinate verified upload corrections across independently committed owners."""
import json

from yt_playlist.core.rating_feedback import Rating, correction_feedback
from yt_playlist.core.track_policy import combined_ratings


class RecordingAssociations:
    def __init__(self, repository, metadata, keys, discovery, calendar, model):
        self.repository, self.metadata, self.keys = repository, metadata, keys
        self.discovery, self.calendar, self.model = discovery, calendar, model

    def associate(self, video, recording, *, source, evidence, now, same_recording=False):
        if not source or not isinstance(evidence, dict) or not evidence:
            raise ValueError('A verified association requires its source and supporting evidence')
        pending = self.repository.association_work(video)
        if pending is not None:
            self._finish(json.loads(pending['plan_json']), pending)
        inputs = self.repository.association_subjects(video, recording)
        previous, target = inputs['previous'], inputs['target']
        if previous == target:
            return
        if same_recording:
            self.metadata.reuse_recording_selections(previous, target)
        old_key, new_key = inputs['previous_key'], inputs['target_key']
        rating_keys = {old_key, new_key}
        ratings_before = self.repository.rating_rows(rating_keys)
        prior_ratings = combined_ratings(ratings_before)
        payload = json.dumps(evidence, sort_keys=True)
        association = self.repository.record_association(video, previous, target, source, payload, now)
        merge_recordings = same_recording and inputs['remaining'] is None
        discovery = self.discovery.prepare_track_reassociation(
            video, previous, target, association, now, merge_recordings=merge_recordings)
        prepared_keys = self.keys.prepare_reassociation(old_key, new_key, previous) if merge_recordings else None
        feedback = correction_feedback(self.repository.rating_feedback_rows(rating_keys), ratings_before,
                                       video, old_key, new_key, merge_recordings=merge_recordings)
        plan = dict(video=video, inputs=inputs, source=source, evidence=payload, now=now,
                    association=association, discovery=discovery, keys=prepared_keys, merge_recordings=merge_recordings,
                    rating_keys=list(prior_ratings), feedback_by_recording=feedback)
        self.repository.save_association_work(video, association, json.dumps(plan, sort_keys=True))
        self._finish(plan, {})

    def _finish(self, plan, completed):
        video, inputs, association = plan['video'], plan['inputs'], plan['association']
        previous, target = inputs['previous'], inputs['target']
        old_key, new_key = inputs['previous_key'], inputs['target_key']
        # Older pending plans may already have rekeyed documents during preparation.
        merge_recordings = plan.get('merge_recordings', plan['keys'] is not None)
        if not completed.get('assigned'):
            self.repository.assign_recording(video, previous, target, association=association)
        if not completed.get('discovery'):
            self.discovery.finish_track_reassociation(plan['discovery'], association=association)
        if merge_recordings:
            if not completed.get('keys'):
                self.keys.finish_reassociation(old_key, new_key, previous, target, plan['keys'], association=association)
            if not completed.get('redirected'):
                self.repository.redirect_recording(previous, target, plan['source'], plan['evidence'], association=association)
            for key in {old_key, new_key, *inputs.get('aliases', [])}:
                self.keys.resolve_mood(key)
        self.calendar.reconcile_recordings({previous, target})
        if not completed.get('model'):
            combined, feedback = {}, {old_key: [], new_key: []}
            if merge_recordings or self.repository.has_upload_rating(video):
                rows = self.repository.rating_rows({old_key, new_key})
                combined = combined_ratings(rows)
                for key in set(plan['rating_keys']) - combined.keys():
                    combined[key] = Rating('INDIFFERENT', plan['now'])
                feedback = plan.get('feedback_by_recording')
                if feedback is None:
                    # Older checkpoints saved undivided feedback before the upload was assigned.
                    original_rows = [dict(row, identity_key=old_key) if row['video_id'] == video else row
                                     for row in rows]
                    feedback = correction_feedback(plan['feedback'], original_rows, video, old_key, new_key,
                                                   merge_recordings=merge_recordings)
            self.model.reassociate_rating_feedback(combined, feedback, association=association)
        self.keys.invalidate_catalog()
        self.repository.finish_association_work(association)
