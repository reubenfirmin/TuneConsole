"""Canonical artist references, ordered credits, and name lookup for display boundaries."""
from contextlib import contextmanager
import json

from yt_playlist.core import artist_credits
from yt_playlist.core.artist_keys import canonical_id
from yt_playlist.repos.base import Repo, lookup_read, synchronized, snapshot_read


class ArtistRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic(join=True):
                # Reserve before selecting a missing identity shared by competing writers.
                self.conn.execute('UPDATE artists SET name=name WHERE 0')
                yield

    @lookup_read
    def reference_rows(self, identities, names):
        """Existing numeric references and exact aliases, queried in bounded batches."""
        references, aliases = {}, {name: set() for name in names}
        redirected = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='artist_redirects'").fetchone()
        for values, column, table in ((list(set(identities)), 'id', 'artists'),
                                      (list(aliases), 'alias', 'artist_aliases')):
            identity_column = 'id' if table == 'artists' else 'artist_id'
            identity = f'COALESCE(d.artist_id,a.{identity_column})' if redirected else f'a.{identity_column}'
            join = f' LEFT JOIN artist_redirects d ON d.retired_id=a.{identity_column}' if redirected else ''
            for offset in range(0, len(values), 400):
                batch = values[offset:offset + 400]
                placeholders = ','.join('?' for _ in batch)
                for row in self.conn.execute(
                        f'SELECT a.{column},{identity} FROM {table} a{join} WHERE a.{column} IN ({placeholders})', batch):
                    if table == 'artists':
                        references[row[0]] = row[1]
                    else:
                        aliases[row[0]].add(row[1])
        return references, aliases

    @snapshot_read
    def alias_rows(self):
        redirected = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='artist_redirects'").fetchone()
        sql = ('SELECT COALESCE(d.artist_id,a.artist_id) artist_id,a.alias FROM artist_aliases a '
               'LEFT JOIN artist_redirects d ON d.retired_id=a.artist_id' if redirected else
               'SELECT artist_id,alias FROM artist_aliases')
        return [dict(row) for row in self.conn.execute(sql)]

    @snapshot_read
    def public_key_inputs(self, identities):
        names = {}
        values = list(set(identities))
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            names.update(self.conn.execute('SELECT id,name FROM artists WHERE id IN (' +
                                           ','.join('?' for _ in batch) + ')', batch))
        return names, self.alias_rows() if values else []

    @synchronized
    def provider_id(self, provider, provider_id):
        row = self.conn.execute('SELECT artist_id FROM artist_provider_ids WHERE provider=? AND provider_id=?',
                                (provider, provider_id)).fetchone()
        return canonical_id(self.conn, row[0]) if row else None

    @synchronized
    def provisional_id(self, name):
        redirects = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='artist_redirects'").fetchone()
        row = self.conn.execute('SELECT id FROM artists WHERE provisional=1 AND name=? ' +
            ('AND NOT EXISTS(SELECT 1 FROM artist_redirects d WHERE d.retired_id=artists.id) ' if redirects else '') +
            'ORDER BY id LIMIT 1', (name,)).fetchone()
        return row[0] if row else None

    @synchronized
    def create(self, name, *, provisional):
        return self.conn.execute('INSERT INTO artists(name,provisional) VALUES (?,?)',
                                  (name, int(provisional))).lastrowid

    @synchronized
    def add_provider(self, identity, provider, provider_id):
        self.conn.execute('INSERT INTO artist_provider_ids VALUES (?,?,?)', (provider, provider_id, identity))

    @synchronized
    def add_alias(self, identity, name, source):
        self.conn.execute('INSERT OR IGNORE INTO artist_aliases VALUES (?,?,?)', (identity, name, source))

    @synchronized
    def identity_inputs(self, identity):
        from yt_playlist.core.legacy_evidence import available
        identity = canonical_id(self.conn, identity)
        row = self.conn.execute('SELECT provisional FROM artists WHERE id=?', (identity,)).fetchone()
        ambiguous = (self.conn.execute('SELECT 1 FROM artist_ambiguities WHERE artist_id=?', (identity,)).fetchone()
                     if available(self.conn) else self.conn.execute(
                         'SELECT 1 FROM identity_state_archive WHERE kind=? AND source_id=?',
                         ('ambiguous_album_artist', str(identity))).fetchone())
        return {'id': identity, 'exists': row is not None, 'provisional': bool(row and row[0]),
                'identified': self.conn.execute('SELECT 1 FROM artist_provider_ids WHERE artist_id=?', (identity,)).fetchone() is not None,
                'ambiguous': ambiguous is not None}

    @synchronized
    def promote(self, identity):
        self.conn.execute('UPDATE artists SET provisional=0 WHERE id=?', (identity,))

    @synchronized
    def credit_subject_candidates(self, existing, name):
        return {canonical_id(self.conn, row['artist_id']) for row in existing
            if row['credit'] == name or self.conn.execute(
                'SELECT 1 FROM artist_aliases WHERE artist_id=? AND alias=?', (row['artist_id'], name)).fetchone()}

    @synchronized
    def has_alias(self, identity, name):
        return self.conn.execute('SELECT 1 FROM artist_aliases WHERE artist_id=? AND alias=?', (identity, name)).fetchone() is not None

    @snapshot_read
    def release_context(self, album_id, previous):
        return {'identity': self.identity_inputs(previous),
            'other_albums': [row[0] for row in self.conn.execute(
                'SELECT album_id FROM album_artists WHERE artist_id=? AND album_id<>?', (previous, album_id))],
            'other_entries': [row[0] for row in self.conn.execute(f'SELECT ta.entry_id FROM {self.entry_artists_table} ta '
                'WHERE ta.artist_id=? AND NOT EXISTS(SELECT 1 FROM album_tracks at '
                'WHERE at.album_id=? AND at.entry_id=ta.entry_id)', (previous, album_id))]}

    @synchronized
    def mark_ambiguity(self, identity):
        from yt_playlist.core.legacy_evidence import mark_artist_ambiguity
        with self.conn.atomic(join=True):
            mark_artist_ambiguity(self.conn, identity)

    @synchronized
    def reconcile_identity(self, previous, target, source, now):
        with self.conn.atomic(join=True):
            self.conn.execute('INSERT INTO artist_redirects VALUES (?,?,?,?)', (previous, target, source, now))
            self.conn.execute('UPDATE artist_redirects SET artist_id=? WHERE artist_id=?', (target, previous))
            typed = artist_credits.available(self.conn)
            if typed:
                artist_credits.reconcile_current(self.conn, previous, target, source, now)
            else:
                self.conn.execute(f'UPDATE {self.entry_artists_table} SET artist_id=? WHERE artist_id=?', (target, previous))
            self.conn.execute('UPDATE album_artists SET artist_id=? WHERE artist_id=?', (target, previous))
            self.conn.execute('INSERT OR IGNORE INTO artist_aliases SELECT ?,alias,source FROM artist_aliases WHERE artist_id=?',
                              (target, previous))
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='spotify_history_listens'").fetchone():
                self.conn.execute('UPDATE main.spotify_history_listens SET artist_id=? WHERE artist_id=?', (target, previous))

    @snapshot_read
    def credit_inputs(self, entry_id):
        row = self.conn.execute('SELECT entry_id FROM legacy_track_refs WHERE legacy_id=?', (entry_id,)).fetchone()
        entry_id = row[0] if row else entry_id
        return {'entry_id': entry_id, 'typed': artist_credits.available(self.conn),
                'artists': [row[0] for row in self.conn.execute(
                    f'SELECT artist_id FROM {self.entry_artists_table} WHERE entry_id=? ORDER BY position', (entry_id,))]}

    @snapshot_read
    def credit_observation_inputs(self, entry_id):
        row = self.conn.execute('SELECT entry_id FROM legacy_track_refs WHERE legacy_id=?', (entry_id,)).fetchone()
        entry_id = row[0] if row else entry_id
        return {'entry_id': entry_id, 'typed': artist_credits.available(self.conn), 'credits': [dict(row) for row in self.conn.execute(
            f'SELECT * FROM {self.entry_artists_table} WHERE entry_id=? ORDER BY position', (entry_id,))]}

    @synchronized
    def store_credit(self, entry_id, credit, structured, source, now, *, typed, delivery_key=None, metadata_delivery_id=None):
        with self.conn.atomic(join=True):
            if typed:
                version = artist_credits.intern(self.conn, credit)
                observation, select = artist_credits.capture(self.conn, entry_id, version, source, now,
                    delivery_key=delivery_key, metadata_delivery_id=metadata_delivery_id)
                if select:
                    artist_credits.select(self.conn, entry_id, observation)
            else:
                self.conn.execute('INSERT INTO artist_credit_observations(entry_id,credit,credits_json,source,observed_at) '
                                  'VALUES (?,?,?,?,?)', (entry_id, credit.display_credit, json.dumps(structured), source, now))
                artist_credits.update_projection(self.conn, entry_id,
                    [(position, member.artist_id, member.credit_text, source) for position, member in enumerate(credit.members)])

    @synchronized
    def artist_row(self, identity):
        row = self.conn.execute('SELECT * FROM artists WHERE id=?', (identity,)).fetchone()
        if row is None:
            return None
        provider_ids = {r['provider']:r['provider_id'] for r in self.conn.execute(
            'SELECT * FROM artist_provider_ids WHERE artist_id=?', (identity,))}
        return {**dict(row), 'provider_ids': provider_ids}

    @snapshot_read
    def names(self):
        return {r[0] for r in self.conn.execute('SELECT DISTINCT name FROM artists')}

    @synchronized
    def album_credits(self, browse_id):
        return [dict(row) for row in self.conn.execute(
            'SELECT aa.*,a.name,ap.provider_id browse_id FROM album_provider_ids p '
            'JOIN album_artists aa ON aa.album_id=p.album_id JOIN artists a ON a.id=aa.artist_id '
            "LEFT JOIN artist_provider_ids ap ON ap.artist_id=a.id AND ap.provider='youtube' "
            "WHERE p.provider='youtube' AND p.provider_id=? ORDER BY aa.position", (browse_id,))]

    @synchronized
    def track_credits(self, entry_id):
        return [dict(r) for r in self.conn.execute(
            f'SELECT ta.*,a.name,ap.provider_id browse_id FROM {self.entry_artists_table} ta '
            'JOIN artists a ON a.id=ta.artist_id LEFT JOIN artist_provider_ids ap '
            "ON ap.artist_id=a.id AND ap.provider='youtube' WHERE entry_id=? ORDER BY position",
            (entry_id,))]

    @synchronized
    def track_artist_ids(self, *, library=False, keys=None):
        keys = list(dict.fromkeys(keys)) if keys is not None else None
        if keys == []:
            return {}
        rows = self.conn.execute(
            'SELECT DISTINCT r.identity_key,ta.artist_id FROM recordings r '
            f'JOIN catalog_entries e ON e.recording_id=r.id JOIN {self.entry_artists_table} ta ON ta.entry_id=e.id '+
            ('JOIN library_memberships m ON m.entry_id=e.id ' if library else '') +
            ('WHERE r.identity_key IN (' + ','.join('?' for _ in keys) + ')' if keys is not None else ''), keys or [])
        out = {}
        for key,aid in rows:
            out.setdefault(key,set()).add(aid)
        return out
