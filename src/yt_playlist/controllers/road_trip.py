"""Recipe decisions and ordered draft publication through SQLite and file access."""
import json
from uuid import uuid4

from yt_playlist.core import road_trip_contract as policy
from yt_playlist.util.genre_catalog import normalize


class RoadTrips:
    def __init__(self, repository, recovery, read_prepared):
        self.repository, self.recovery, self.read_prepared = repository, recovery, read_prepared

    def list_road_trip_recipes(self):
        return self.repository.list_road_trip_recipes()

    def get_road_trip_recipe(self, recipe_id):
        return self.repository.get_road_trip_recipe(recipe_id)

    def save_road_trip_recipe(self, recipe_id, name, own_pct, artists, genres,
                              target_minutes, now, familiarity_pct=policy.DEFAULT_FAMILIARITY_PCT,
                              blacklist_genres=None):
        policy.validate_recipe_values(own_pct, familiarity_pct, target_minutes, now)
        excluded = blacklist_genres or []
        values = dict(name=name, own_pct=own_pct, target_minutes=target_minutes, familiarity_pct=familiarity_pct,
                      updated_at=now, artists=artists, genres=genres, blacklist_genres=excluded)
        if not self.repository.uses_memberships():
            return self.repository.save_recipe(recipe_id, values)
        policy.validate_selections(artists, genres, excluded)
        with self.repository.transaction():
            resolved_artists = []
            for label in artists:
                identity = None
                if label.startswith('@artist:'):
                    try:
                        identity = int(label.removeprefix('@artist:'))
                    except ValueError:
                        resolved_artists.append((label, None))
                        continue
                candidates = self.repository.artist_candidates(label, artist_id=identity)
                resolved_artists.append((label, policy.select_identity(candidates)))
            resolved_genres, custom = [], {}
            for role, labels in (('include', genres), ('exclude', excluded)):
                identities = []
                for label in labels:
                    candidates = self.repository.genre_candidates(label, normalize(label))
                    identity = policy.select_identity(candidates, ambiguous='Ambiguous genre label: '+label)
                    if identity is None:
                        identity = policy.custom_genre_key(label)
                        if identity not in custom:
                            if self.repository.genre_key_exists(identity):
                                raise ValueError('Custom genre key collides with an existing identity: '+label)
                            custom[identity] = label
                    identities.append(identity)
                resolved_genres.append((role, identities))
            memberships = policy.membership_rows(resolved_artists, resolved_genres)
            for key, label in custom.items():
                self.repository.insert_custom_genre(key, label)
            return self.repository.save_recipe(recipe_id, values, memberships=memberships)

    def delete_road_trip_recipe(self, recipe_id):
        self.repository.delete_road_trip_recipe(recipe_id)

    def _read(self, facts):
        if facts is None:
            return None
        if 'row' in facts:
            row = facts['row']
            state = self.recovery.read((row['recovery_filename'], row['recovery_digest'], row['recovery_bytes']),
                                       kind='road-trip-draft', owner=row['id'])
            return policy.restore_draft(state, row['revision'], facts['selections'])
        if 'prepared' in facts:
            return (json.loads(self.read_prepared(facts['prepared'], facts['parts']))
                    if facts['prepared'] is not None else None)
        return json.loads(facts['raw'])

    def get_road_trip_draft(self, recipe_id):
        with self.recovery.retained():
            return self._read(self.repository.draft_facts(recipe_id))

    def latest_road_trip_draft(self):
        with self.recovery.retained():
            facts = self.repository.draft_facts(latest=True)
            return {'recipe_id':facts['recipe_id'], 'state':self._read(facts)} if facts else None

    def save_road_trip_draft(self, recipe_id, state, now):
        self._save(recipe_id, state, now)

    def replace_road_trip_draft(self, recipe_id, state, now, *, expected_revision):
        return self._save(recipe_id, state, now, expected_revision=expected_revision, conditional=True)

    def _save(self, recipe_id, state, now, *, expected_revision=None, conditional=False):
        if not self.repository.uses_draft_files():
            with self.recovery.retained():
                facts = self.repository.draft_facts(recipe_id, include_raw=True) if conditional else None
                if conditional:
                    current = self._read(facts)
                    if current is None or current.get('revision') != expected_revision:
                        return False
                return self.repository.save_legacy_draft(recipe_id, json.dumps(state), now,
                    expected_raw=facts['raw'] if conditional else None, conditional=conditional)
        with self.recovery.retained():
            for _ in range(3):
                with self.repository.transaction():
                    current = self.repository.draft_owner(recipe_id)
                    if conditional and (current is None or current['revision'] != expected_revision):
                        return False
                    owner = current['id'] if current else uuid4().hex
                    account = current['account_id'] if current else self.repository.master_account()
                    if current is None and account is None:
                        raise ValueError('Choose an active account before creating a draft')
                    revision, status, recovery, selected = policy.draft_document(state)
                    selections = []
                    entries = {}
                    for kind, position, video, candidate in selected:
                        if video not in entries:
                            entries[video] = self.repository.captured_video(video, identity_key='@unresolved-video:'+video,
                                title=candidate.get('title') or '', artist=candidate.get('artist') or '', now=now)
                        entry, recording = entries[video]
                        selections.append((kind, position, recording, entry, video))
                reference = self.recovery.publish('road-trip-draft', owner, recovery)
                if self.repository.save_draft(recipe_id, owner, account, revision, status, reference, selections, now,
                        expected_revision=expected_revision, conditional=conditional):
                    return True
                if conditional:
                    return False
            raise RuntimeError('Draft identity changed repeatedly during publication')

    def delete_road_trip_draft(self, recipe_id):
        self.repository.delete_road_trip_draft(recipe_id)

    def set_road_trip_last_playlist(self, recipe_id, playlist_ytm):
        self.repository.set_road_trip_last_playlist(recipe_id, playlist_ytm)
