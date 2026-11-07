"""SQLite recipes, draft references and ordered exact-upload selections."""
from contextlib import contextmanager
import json

from yt_playlist.core import recipe_memberships
from yt_playlist.core.artist_keys import canonical_id
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read


class RoadTripRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic(join=True):
                self.conn.execute('UPDATE road_trip_recipes SET name=name WHERE 0')
                yield

    @lookup_read
    def uses_memberships(self):
        return recipe_memberships.available(self.conn)

    @lookup_read
    def uses_draft_files(self):
        return any(row[1] == 'recovery_filename' for row in self.conn.execute('PRAGMA table_info(road_trip_drafts)'))

    @snapshot_read
    def list_road_trip_recipes(self):
        return [self._recipe(row) for row in self.conn.execute(
            'SELECT '+self._recipe_columns()+' FROM road_trip_recipes ORDER BY updated_at DESC').fetchall()]

    @snapshot_read
    def get_road_trip_recipe(self, recipe_id):
        row = self.conn.execute('SELECT '+self._recipe_columns()+
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
        rows = self.conn.execute('SELECT key FROM genre_catalog WHERE key=? OR name=? COLLATE NOCASE '
            'UNION SELECT genre_key FROM genre_alias WHERE label_key=? OR label=? COLLATE NOCASE',
            (label, label, normalized, label)).fetchall()
        redirected = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_redirects'").fetchone()
        keys = set()
        for row in rows:
            current = self.conn.execute('SELECT genre_key FROM genre_redirects WHERE retired_key=?',
                (row[0],)).fetchone() if redirected else None
            keys.add(current[0] if current else row[0])
        return keys

    @synchronized
    def genre_key_exists(self, key):
        return self.conn.execute('SELECT 1 FROM genre_catalog WHERE key=?', (key,)).fetchone() is not None

    @synchronized
    def insert_custom_genre(self, key, label):
        self.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')", (key, label))
        self.conn.execute("INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,'custom')",
                          (key, label, key))

    @synchronized
    def save_recipe(self, recipe_id, values, *, memberships=None):
        with self.conn.atomic(join=True):
            fields = 'name,own_pct,target_minutes,familiarity_pct,updated_at'
            arguments = [values[field] for field in fields.split(',')]
            if memberships is None:
                fields += ',artists,genres,blacklist_genres'
                arguments += [json.dumps(values[field]) for field in ('artists', 'genres', 'blacklist_genres')]
            if recipe_id is None:
                recipe_id = self.conn.execute('INSERT INTO road_trip_recipes('+fields+',created_at) VALUES ('+
                    ','.join('?' for _ in arguments)+',?)', (*arguments, values['updated_at'])).lastrowid
            else:
                changed = self.conn.execute('UPDATE road_trip_recipes SET '+
                    ','.join(field+'=?' for field in fields.split(','))+' WHERE id=?', (*arguments, recipe_id)).rowcount
                if memberships is not None and not changed:
                    raise ValueError('Unknown recipe')
            if memberships is not None:
                for table in ('recipe_artists', 'recipe_genres', 'recipe_unresolved_artists', 'recipe_unresolved_genres'):
                    self.conn.execute(f'DELETE FROM {table} WHERE recipe_id=?', (recipe_id,))
                for field, columns in (('artists', 3), ('genres', 4), ('unresolved_artists', 4), ('unresolved_genres', 5)):
                    table, rows = 'recipe_'+field, memberships[field]
                    self.conn.executemany(f'INSERT INTO {table} VALUES ('+','.join('?' for _ in range(columns))+')',
                                          [(recipe_id, *row) for row in rows])
            return recipe_id

    @synchronized
    def delete_road_trip_recipe(self, recipe_id):
        with self.conn.atomic(join=True):
            self.conn.execute('DELETE FROM road_trip_recipes WHERE id=?', (recipe_id,))
            self.conn.execute('DELETE FROM road_trip_drafts WHERE recipe_id=?', (recipe_id,))

    @synchronized
    def draft_owner(self, recipe_id):
        row = self.conn.execute('SELECT id,account_id,revision FROM road_trip_drafts WHERE recipe_id=?',
                                (recipe_id,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def master_account(self):
        row = self.conn.execute('SELECT id FROM accounts WHERE active=1 AND is_master=1').fetchone()
        return row[0] if row else None

    @synchronized
    def captured_video(self, video, *, identity_key, title, artist, now):
        row = self.conn.execute('SELECT id,recording_id FROM catalog_entries WHERE video_id=? ORDER BY id LIMIT 1',
                                (video,)).fetchone()
        if row is None:
            recording = self.conn.execute('INSERT INTO recordings(identity_key,title,artist,provisional,created_at) '
                'VALUES (?,?,?,1,?)', (identity_key, title, artist, now)).lastrowid
            self.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist) VALUES (?,?,?,?)',
                              (video, recording, title, artist))
            entry = self.conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?)',
                                      (recording, video)).lastrowid
            return entry, recording
        return tuple(row)

    @snapshot_read
    def draft_facts(self, recipe_id=None, *, latest=False, include_raw=False):
        if self.uses_draft_files():
            row = self.conn.execute('SELECT * FROM road_trip_drafts ORDER BY updated_at DESC,id DESC LIMIT 1'
                if latest else 'SELECT * FROM road_trip_drafts WHERE recipe_id=?',
                () if latest else (recipe_id,)).fetchone()
            if row is None:
                return None
            return {'recipe_id':row['recipe_id'], 'row':dict(row),
                'selections':list(map(tuple, self.conn.execute(
                    'SELECT selection_state,video_id FROM road_trip_draft_entries '
                    'WHERE draft_id=? ORDER BY selection_state,position', (row['id'],))))}
        from yt_playlist.core.preparation import draft_publication
        from yt_playlist.core.preparation import road_trip_drafts as prepared_drafts
        owner = draft_publication.OWNER
        stage = owner.selected(self.conn)
        columns = 'd.recipe_id' + (',d.state' if stage is None or include_raw else '')
        row = self.conn.execute('SELECT '+columns+' FROM road_trip_drafts d '
            'JOIN road_trip_recipes r ON r.id=d.recipe_id ORDER BY d.updated_at DESC LIMIT 1' if latest else
            'SELECT '+columns+' FROM road_trip_drafts d WHERE recipe_id=?',
            () if latest else (recipe_id,)).fetchone()
        if row is None:
            return None
        facts = {'recipe_id':row['recipe_id']}
        if 'state' in row.keys():
            facts['raw'] = row['state']
        if stage is not None:
            header = owner.capture(self.conn, stage, row['recipe_id'])
            facts['prepared'] = dict(header) if header else None
            facts['parts'] = list(map(tuple, self.conn.execute('SELECT position,filename,digest,byte_size FROM '+
                prepared_drafts.PARTS+' WHERE recipe_id=? AND generation=? ORDER BY position LIMIT ?',
                (row['recipe_id'], header['generation'], prepared_drafts.MAX_PARTS+1)))) if header else []
        return facts

    @synchronized
    def save_draft(self, recipe_id, owner, account_id, revision, status, reference, selections, now,
                   *, expected_revision=None, conditional=False):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE road_trip_drafts SET revision=revision WHERE 0')
            current = self.draft_owner(recipe_id)
            if conditional and (current is None or current['revision'] != expected_revision):
                return False
            if current is not None and current['id'] != owner:
                return False
            self.conn.execute('INSERT INTO road_trip_drafts(id,recipe_id,account_id,legacy_author_unknown,revision,state,'
                'recovery_filename,recovery_digest,recovery_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(recipe_id) DO UPDATE SET revision=excluded.revision,state=excluded.state,'
                'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,'
                'recovery_bytes=excluded.recovery_bytes,updated_at=excluded.updated_at',
                (owner, recipe_id, account_id, int(account_id is None), revision, status, *reference, now))
            self.conn.execute('DELETE FROM road_trip_draft_entries WHERE draft_id=?', (owner,))
            self.conn.executemany('INSERT INTO road_trip_draft_entries(draft_id,selection_state,position,recording_id,'
                'entry_id,video_id) VALUES (?,?,?,?,?,?)', [(owner, *row) for row in selections])
            return True

    @synchronized
    def save_legacy_draft(self, recipe_id, raw, now, *, expected_raw=None, conditional=False):
        with self.conn.atomic(join=True):
            if conditional:
                return bool(self.conn.execute('UPDATE road_trip_drafts SET state=?,updated_at=? '
                    'WHERE recipe_id=? AND state=?', (raw, now, recipe_id, expected_raw)).rowcount)
            self.conn.execute('INSERT INTO road_trip_drafts(recipe_id,state,updated_at) VALUES (?,?,?) '
                'ON CONFLICT(recipe_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at',
                (recipe_id, raw, now))
            return True

    @synchronized
    def delete_road_trip_draft(self, recipe_id):
        self.conn.execute('DELETE FROM road_trip_drafts WHERE recipe_id=?', (recipe_id,))
        self.conn.commit()

    @synchronized
    def set_road_trip_last_playlist(self, recipe_id, playlist_ytm):
        self.conn.execute('UPDATE road_trip_recipes SET last_playlist_id=? WHERE id=?', (playlist_ytm, recipe_id))
        self.conn.commit()

    def _recipe_columns(self):
        from yt_playlist.core.preparation import recipe_publication
        columns = 'id,name,own_pct,target_minutes,familiarity_pct,last_playlist_id,created_at,updated_at'
        if not recipe_memberships.available(self.conn) and recipe_publication.OWNER.selected(self.conn) is None:
            columns += ',artists,genres,blacklist_genres'
        return columns

    def _recipe(self, row):
        from yt_playlist.core.preparation import recipe_publication
        if recipe_memberships.available(self.conn):
            selections = recipe_memberships.read(self.conn, row['id'])
        else:
            selections = recipe_publication.read(self.conn, row['id'])
            if selections is None:
                selections = {field:json.loads(row[field]) for field in ('artists', 'genres', 'blacklist_genres')}
        return {**dict(row), **selections}
