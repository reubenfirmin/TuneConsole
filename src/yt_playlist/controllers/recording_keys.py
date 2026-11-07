"""Coordinate alias disambiguation and saved documents before changing a catalog key."""
import json

from yt_playlist.core.catalog_state import rekey_state
from yt_playlist.core.artist_policy import unique
from yt_playlist.util.matching import normalize


class RecordingKeys:
    def __init__(self, repository, originals, recipes, road_trips, calendar):
        self.repository, self.originals = repository, originals
        self.recipes, self.road_trips = recipes, road_trips
        self.calendar = calendar

    def add_alias(self, alias, recording, source):
        owners = self.repository.add_alias(alias, recording, source)
        renamed = False
        if len(owners) > 1:
            for owner in owners:
                if owner['identity_key'] == alias:
                    key = f"@recording:{owner['id']}|" + normalize(owner['artist'] or '')
                    self._rename(alias, key, owner['id'])
                    renamed = True
        self.resolve_mood(alias)
        if renamed:
            self.repository.invalidate_catalog()

    def resolve_mood(self, alias):
        candidates = self.repository.key_candidates(alias)
        target = self._target(candidates)
        self.repository.resolve_mood_seeds(alias, target,
            None if target is not None else 'ambiguous_alias' if candidates['aliases'] else 'unknown_recording')

    def invalidate_catalog(self):
        self.repository.invalidate_catalog()

    @staticmethod
    def _target(candidates):
        return candidates['direct'] if candidates['direct'] is not None else unique(candidates['aliases'])

    def _rename(self, old_key, new_key, recording):
        inputs, modes = self.prepare_reassociation(old_key, new_key, recording)
        self.repository.apply_rekey(old_key, new_key, recording, inputs['feedback'], modes)
        self.calendar.reconcile_recordings({recording})

    def prepare_reassociation(self, old_key, new_key, recording):
        inputs = self.repository.rekey_inputs(recording)
        for row in inputs['feedback']:
            self.originals.retain('recommendation', 'feedback_reassociation', f"{recording}:{row['rowid']}:{old_key}",
                json.dumps(row), 'verified association or alias disambiguation; learning is not replayed')
        modes = []
        for row in inputs['modes']:
            try:
                revised = rekey_state(json.loads(row['rep_keys']), {old_key: new_key})
            except (ValueError, TypeError):
                continue
            modes.append((json.dumps(revised), row['mode_id']))
        self.road_trips.rekey_recordings({old_key: new_key})
        self.recipes.rekey_recordings({old_key: new_key})
        return inputs, modes

    def finish_reassociation(self, old_key, new_key, previous, target, prepared):
        inputs, modes = prepared
        self.resolve_mood(old_key)
        feedback = [row for row in inputs['feedback'] if row['kind'] not in ('like', 'dislike', 'like_grad')]
        self.repository.apply_rekey(old_key, new_key, target, feedback, modes, previous=previous)

    def resolve_listening(self, recording):
        keys = [key for key in self.repository.listening_keys(recording)
                if self._target(self.repository.key_candidates(key)) == recording]
        if self.repository.resolve_listening_keys(recording, keys):
            self.calendar.reconcile_recordings({recording})
