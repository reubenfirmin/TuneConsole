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
        self.repository.assign_recording(video, previous, target, new_key)
        self.discovery.finish_track_reassociation(discovery)
        if retired:
            self.keys.finish_reassociation(old_key, new_key, previous, target, prepared_keys)
            aliases = self.repository.redirect_recording(previous, target, source, payload)
            for key in {old_key, *aliases}:
                self.keys.resolve_mood(key)
        self.calendar.reconcile_recordings({previous, target})
        combined = combined_ratings(self.repository.rating_rows(rating_keys))
        for key in prior_ratings.keys() - combined.keys():
            combined[key] = Rating('INDIFFERENT', now)
        self.model.reassociate_rating_feedback(combined, {
            old_key: [] if retired else [row for row in feedback if row['item_key'] == old_key], new_key: feedback})
        self.keys.invalidate_catalog()
