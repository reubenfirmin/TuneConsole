"""SQLite recipes, draft references and ordered exact-upload selections."""
from contextlib import contextmanager
import json

from yt_playlist.core import genre_identity, road_trip_selections, track_sources
from yt_playlist.core.artist_keys import canonical_id
from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from yt_playlist.repos import collection_file_writes

_RECIPE_COLUMNS = 'id,name,own_pct,target_minutes,familiarity_pct,last_playlist_id,created_at,updated_at,selections'


class RoadTripRepo(Repo):
    @synchronized
    def reserve_draft_file(self, owner, integrity):
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, 'road-trip-draft', owner, integrity)

    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic(join=True):
                self.conn.execute('UPDATE road_trip_recipes SET name=name WHERE 0')
                yield

    @snapshot_read
    def list_road_trip_recipes(self):
        return [self._recipe(row) for row in self.conn.execute(
            'SELECT ' + _RECIPE_COLUMNS + ' FROM road_trip_recipes ORDER BY updated_at DESC').fetchall()]

    @snapshot_read
    def get_road_trip_recipe(self, recipe_id):
        row = self.conn.execute('SELECT ' + _RECIPE_COLUMNS +
            ' FROM road_trip_recipes WHERE id=?', (recipe_id,)).fetchone()
        return self._recipe(row) if row else None

    @synchronized
    def artist_candidates(self, label, *, artist_id=None):
        if artist_id is not None:
            rows = self.conn.execute('SELECT id FROM artists WHERE id=?', (artist_id,))
        else:
            rows = self.conn.execute('SELECT DISTINCT artist_id FROM artist_aliases WHERE alias=?', (label,))
        return {canonical_id(self.conn, row[0]) for row in rows.fetchall()}

    @synchronized
    def genre_candidates(self, label, normalized):
        return genre_identity.matching_keys(self.conn, label, normalized)

    @synchronized
    def genre_key_exists(self, key):
        return self.conn.execute('SELECT 1 FROM genre_catalog WHERE key=?', (key,)).fetchone() is not None

    @synchronized
    def insert_custom_genre(self, key, label):
        self.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", (key, label))
        self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom')",
                          (key, label, key))

    @synchronized
    def save_recipe(self, recipe_id, values, *, selections):
        with self.conn.atomic(join=True):
            fields = ('name', 'own_pct', 'target_minutes', 'familiarity_pct', 'updated_at', 'selections')
            arguments = [values[field] for field in fields[:-1]] + [json.dumps(selections)]
            if recipe_id is None:
                recipe_id = self.conn.execute('INSERT INTO road_trip_recipes('+','.join(fields)+',created_at) VALUES ('+
                    ','.join('?' for _ in arguments)+',?)', (*arguments, values['updated_at'])).lastrowid
            else:
                changed = self.conn.execute('UPDATE road_trip_recipes SET '+
                    ','.join(field+'=?' for field in fields)+' WHERE id=?', (*arguments, recipe_id)).rowcount
                if not changed:
                    raise ValueError('Unknown recipe')
            return recipe_id

    @synchronized
    def delete_road_trip_recipe(self, recipe_id):
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM road_trip_recipes WHERE id=?', (recipe_id,))

    @synchronized
    def draft_owner(self, recipe_id):
        row = self.conn.execute('SELECT draft_id AS id,draft_account_id AS account_id,draft_revision AS revision '
                                'FROM road_trip_recipes WHERE id=? AND draft_id IS NOT NULL', (recipe_id,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def master_account(self):
        row = self.conn.execute('SELECT id FROM accounts WHERE active=1 AND is_master=1').fetchone()
        return row[0] if row else None

    @synchronized
    def captured_video(self, video, *, identity_key, title, artist, now):
        if not self.conn.execute('SELECT 1 FROM track_sources WHERE video_id=?', (video,)).fetchone():
            recording = self.conn.execute('INSERT INTO recordings(identity_key,title,artist,provisional,created_at) '
                'VALUES (?,?,?,1,?)', (identity_key, title, artist, now)).lastrowid
            track_sources.create(self.conn, recording, video, None, title, artist, retain_original=False)

    @snapshot_read
    def draft_facts(self, recipe_id=None, *, latest=False):
        condition = 'ORDER BY draft_updated_at DESC,draft_id DESC LIMIT 1' if latest else 'AND id=?'
        row = self.conn.execute('SELECT id AS recipe_id,draft_id AS id,draft_revision AS revision,'
            'recovery_filename,recovery_digest,recovery_bytes,draft_selections '
            f'FROM road_trip_recipes WHERE draft_id IS NOT NULL {condition}', () if latest else (recipe_id,)).fetchone()
        if row is None:
            return None
        facts = dict(row)
        selections = [tuple(selection) for selection in json.loads(facts.pop('draft_selections'))]
        return {'recipe_id': row['recipe_id'], 'row': facts, 'selections': selections}

    @synchronized
    def save_draft(self, recipe_id, owner, account_id, revision, status, reference, selections, now,
                   *, file_id, expected_revision=None, conditional=False):
        with self.conn.atomic():
            changed = self.conn.execute(
                'UPDATE road_trip_recipes SET draft_id=?,'
                'draft_account_id=CASE WHEN draft_id IS NULL THEN ? ELSE draft_account_id END,'
                'draft_revision=?,draft_state=?,recovery_filename=?,recovery_digest=?,recovery_bytes=?,'
                'draft_updated_at=?,draft_selections=? WHERE id=? AND (draft_id IS NULL OR draft_id=?) '
                'AND (NOT ? OR draft_revision=?)',
                (owner, account_id, revision, status, *reference, now, json.dumps(selections), recipe_id, owner,
                 conditional, expected_revision)).rowcount
            if not changed:
                if not conditional and not self.conn.execute(
                    'SELECT 1 FROM road_trip_recipes WHERE id=?', (recipe_id,)
                ).fetchone():
                    raise ValueError('Unknown recipe')
                return False
            collection_file_writes.complete(self.conn, file_id, 'road-trip-draft', owner, reference)
            return True

    @snapshot_read
    def draft_updated_ats(self):
        return dict(self.conn.execute('SELECT id,draft_updated_at FROM road_trip_recipes WHERE draft_id IS NOT NULL'))

    @synchronized
    def rekey_draft_reference(self, identity, expected_revision, revision, reference, *, file_id):
        with self.conn.atomic():
            changed = bool(self.conn.execute('UPDATE road_trip_recipes SET draft_revision=?,recovery_filename=?,'
                'recovery_digest=?,recovery_bytes=? WHERE draft_id=? AND draft_revision=?',
                (revision, *reference, identity, expected_revision)).rowcount)
            if changed:
                collection_file_writes.complete(self.conn, file_id, 'road-trip-draft', identity, reference)
            return changed

    @synchronized
    def delete_road_trip_draft(self, recipe_id):
        self.conn.execute('UPDATE road_trip_recipes SET draft_id=NULL,draft_account_id=NULL,draft_revision=NULL,'
            'draft_state=NULL,draft_updated_at=NULL,recovery_filename=NULL,recovery_digest=NULL,recovery_bytes=NULL,'
            'draft_selections=NULL WHERE id=? AND draft_id IS NOT NULL', (recipe_id,))
        self.conn.commit()

    @synchronized
    def set_road_trip_last_playlist(self, recipe_id, playlist_ytm):
        self.conn.execute('UPDATE road_trip_recipes SET last_playlist_id=? WHERE id=?', (playlist_ytm, recipe_id))
        self.conn.commit()

    def _recipe(self, row):
        recipe = dict(row)
        recipe.update(road_trip_selections.project(self.conn, recipe.pop('selections')))
        return recipe
