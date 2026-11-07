"""Coordinate verified upload corrections across independently committed owners."""
import json

from yt_playlist.core.rating_feedback import Rating
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
        prior_ratings = combined_ratings(self.repository.rating_rows(rating_keys))
        feedback = self.repository.rating_feedback_rows(rating_keys)
        payload = json.dumps(evidence, sort_keys=True)
        association = self.repository.record_association(video, previous, target, source, payload, now)
        discovery = self.discovery.prepare_track_reassociation(video, previous, target, association, now)
        retired = inputs['remaining'] is None
        prepared_keys = self.keys.prepare_reassociation(old_key, new_key, previous) if retired else None
        plan = dict(video=video, inputs=inputs, source=source, evidence=payload, now=now,
                    association=association, discovery=discovery, keys=prepared_keys,
                    rating_keys=list(prior_ratings), feedback=feedback)
        self.repository.save_association_work(video, association, json.dumps(plan, sort_keys=True))
        self._finish(plan, {})

    def _finish(self, plan, completed):
        video, inputs, association = plan['video'], plan['inputs'], plan['association']
        previous, target = inputs['previous'], inputs['target']
        old_key, new_key = inputs['previous_key'], inputs['target_key']
        retired = inputs['remaining'] is None
        if not completed.get('assigned'):
            self.repository.assign_recording(video, previous, target, new_key, association=association)
        if not completed.get('discovery'):
            self.discovery.finish_track_reassociation(plan['discovery'], association=association)
        if retired:
            if not completed.get('keys'):
                self.keys.finish_reassociation(old_key, new_key, previous, target, plan['keys'], association=association)
            if not completed.get('redirected'):
                self.repository.redirect_recording(previous, target, plan['source'], plan['evidence'], association=association)
            for key in {old_key, new_key, *inputs.get('aliases', [])}:
                self.keys.resolve_mood(key)
        self.calendar.reconcile_recordings({previous, target})
        if not completed.get('model'):
            combined = combined_ratings(self.repository.rating_rows({old_key, new_key}))
            for key in set(plan['rating_keys']) - combined.keys():
                combined[key] = Rating('INDIFFERENT', plan['now'])
            feedback = plan['feedback']
            self.model.reassociate_rating_feedback(combined, {
                old_key: [] if retired else [row for row in feedback if row['item_key'] == old_key], new_key: feedback},
                association=association)
        self.keys.invalidate_catalog()
        self.repository.finish_association_work(association)
