"""Canonical artist references, ordered credits, and name lookup for display boundaries."""
from contextlib import contextmanager

from yt_playlist.core import artists
from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class ArtistRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic(join=True):
                # Reserve before selecting a missing identity shared by competing writers.
                self.conn.execute('UPDATE artists SET name=name WHERE 0')
                yield

    @synchronized
    def resolve(self, value):
        return artists.lookup(self.conn,value)

    @synchronized
    def provider_id(self, provider, provider_id):
        row = self.conn.execute('SELECT artist_id FROM artist_provider_ids WHERE provider=? AND provider_id=?',
                                (provider, provider_id)).fetchone()
        return artists.canonical_id(self.conn, row[0]) if row else None

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
    def get_artist(self, value):
        aid = artists.lookup(self.conn,value)
        row = self.conn.execute('SELECT * FROM artists WHERE id=?', (aid,)).fetchone()
        if row is None:
            return None
        provider_ids = {r['provider']:r['provider_id'] for r in self.conn.execute(
            'SELECT * FROM artist_provider_ids WHERE artist_id=?', (aid,))}
        return {**dict(row),'provider_ids':provider_ids,'key':artists.token(aid)}

    @synchronized
    def key(self, value):
        aid = artists.lookup(self.conn,value)
        return artists.token(aid) if aid else None

    @synchronized
    def public_key(self, value):
        aid = artists.lookup(self.conn,value)
        return artists.public_key(self.conn,aid) if aid else value

    @snapshot_read
    def public_keys(self, values):
        values = list(values)
        resolved = {value: artists.lookup(self.conn, value) for value in values}
        keys = artists.public_keys(self.conn, [aid for aid in resolved.values() if aid is not None])
        return {value: keys[aid] if aid is not None else value for value, aid in resolved.items()}

    @snapshot_read
    def name_keys(self):
        """Resolve display names on one private connection, including ambiguous-name behavior."""
        names = {r[0] for r in self.conn.execute("SELECT name FROM artists")}
        return self.public_keys(names)

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
    def track_keys(self, *, library=False, keys=None):
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
            out.setdefault(key,set()).add(artists.token(aid))
        return out
