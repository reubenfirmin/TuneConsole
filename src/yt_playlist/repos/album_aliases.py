"""Database facts and collection-row changes for verified album aliases."""
from yt_playlist.core import album_memberships, album_tracklists
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class AlbumAliasesRepo(Repo):
    @lookup_read
    def fingerprint_inputs(self, browse):
        row = self.conn.execute('SELECT p.album_id,f.digest FROM album_provider_ids p '
            'LEFT JOIN album_provider_fingerprints f USING(provider,provider_id) '
            "WHERE p.provider='youtube' AND p.provider_id=?", (browse,)).fetchone()
        shared = row is not None and self.conn.execute('SELECT 1 FROM album_provider_ids '
            "WHERE provider='youtube' AND album_id=? AND provider_id<>? LIMIT 1",
            (row['album_id'], browse)).fetchone() is not None
        return {'shared': shared, 'digest': row['digest'] if row else None}

    @synchronized
    def remember_fingerprint(self, browse, album_id, digest, now):
        with self.conn.atomic():
            self.conn.execute("INSERT INTO album_provider_fingerprints VALUES ('youtube',?,?,?) "
                'ON CONFLICT(provider,provider_id) DO UPDATE SET digest=excluded.digest,observed_at=excluded.observed_at',
                (browse, digest, now))
            return [row[0] for row in self.conn.execute('SELECT DISTINCT p.album_id '
                'FROM album_provider_fingerprints f JOIN album_provider_ids p USING(provider,provider_id) '
                "WHERE f.provider='youtube' AND f.digest=? AND p.album_id<>? ORDER BY p.album_id", (digest, album_id))]

    def _track_inputs(self, album_id):
        revisions = []
        if album_tracklists.available(self.conn):
            for row in self.conn.execute('SELECT id,status,order_status FROM album_tracklists WHERE album_id=? '
                    "AND capture_kind IN ('provider','legacy_full')", (album_id,)):
                revisions.append({**dict(row), 'occurrences': [dict(item) for item in self.conn.execute(
                    'SELECT o.position,coalesce(e.video_id,o.raw_source_ref) video_id '
                    'FROM album_track_occurrences o LEFT JOIN album_occurrence_sources s ON s.occurrence_id=o.id '
                    'LEFT JOIN catalog_entries e ON e.id=s.entry_id WHERE o.tracklist_id=? ORDER BY o.position,o.id',
                    (row['id'],))]})
        tracks = [dict(row) for row in self.conn.execute('SELECT t.*,e.video_id FROM album_tracks t '
            'LEFT JOIN catalog_entries e ON e.id=t.entry_id WHERE t.album_id=? '
            'ORDER BY t.position IS NULL,t.position,t.id', (album_id,))]
        state = self.conn.execute('SELECT complete,expected_track_count FROM album_fetch_state WHERE album_id=?',
                                  (album_id,)).fetchone()
        return {'revisions': revisions, 'tracks': tracks, 'state': dict(state) if state else None}

    @snapshot_read
    def merge_inputs(self, target, retired):
        conflict = self.conn.execute('SELECT 1 FROM album_provider_ids a JOIN album_provider_ids b '
            "ON a.provider=b.provider AND a.provider<>'youtube' AND a.provider_id<>b.provider_id "
            'WHERE a.album_id=? AND b.album_id=? LIMIT 1', (target, retired)).fetchone() is not None
        memberships = ('account_albums', 'unattributed_album_memberships') if album_memberships.available(self.conn) else (
            'album_library_memberships',)
        tables = ('albums', 'album_provider_ids', 'album_artists', *memberships,
                  'album_discovery_memberships', 'album_tracks', 'album_fetch_state')
        original = {table: [dict(row) for row in self.conn.execute(
            f'SELECT * FROM {table} WHERE {"id" if table == "albums" else "album_id"} IN (?,?)',
            (target, retired))] for table in tables}
        return {'conflict': conflict, 'target': self._track_inputs(target),
                'retired': self._track_inputs(retired), 'original': original}

    def _move_memberships(self, target, retired):
        if album_memberships.available(self.conn):
            album_memberships.merge(self.conn, target, retired)
            return
        self.conn.execute('UPDATE album_library_memberships AS current SET saved_at=('
            'SELECT MIN(saved_at) FROM album_library_memberships old WHERE old.album_id IN (?,?) '
            'AND old.account_id IS current.account_id) WHERE album_id=?', (target, retired, target))
        self.conn.execute('DELETE FROM album_library_memberships AS old WHERE album_id=? AND EXISTS('
            'SELECT 1 FROM album_library_memberships current WHERE current.album_id=? '
            'AND current.account_id IS old.account_id)', (retired, target))
        self.conn.execute('UPDATE album_library_memberships SET album_id=? WHERE album_id=?', (target, retired))
        self.conn.execute('INSERT INTO album_library_revisions SELECT id,1 FROM accounts WHERE 1 '
            'ON CONFLICT(account_id) DO UPDATE SET revision=revision+1')

    def _move_legacy_tracks(self, target, retired):
        has_full = self.conn.execute('SELECT 1 FROM album_tracks WHERE album_id=? AND is_full=1', (target,)).fetchone()
        if has_full:
            self.conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=1', (retired,))
        else:
            self.conn.execute('UPDATE album_tracks SET album_id=? WHERE album_id=? AND is_full=1', (target, retired))
        self.conn.execute('DELETE FROM album_tracks AS old WHERE album_id=? AND EXISTS('
            'SELECT 1 FROM album_tracks current WHERE current.album_id=? AND current.entry_id=old.entry_id)',
            (retired, target))
        self.conn.execute('UPDATE album_tracks SET album_id=? WHERE album_id=?', (target, retired))
        self.conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=0 AND entry_id IN '
            '(SELECT entry_id FROM album_tracks WHERE album_id=? AND is_full=1)', (target, target))
        if not has_full:
            self.conn.execute('DELETE FROM album_fetch_state WHERE album_id=?', (target,))
            self.conn.execute('UPDATE album_fetch_state SET album_id=? WHERE album_id=?', (target, retired))
        self.conn.execute('DELETE FROM album_fetch_state WHERE album_id=?', (retired,))

    @synchronized
    def apply_merge(self, target, retired, digest, now):
        with self.conn.atomic():
            self._move_memberships(target, retired)
            typed = album_tracklists.available(self.conn)
            if not typed:
                self._move_legacy_tracks(target, retired)
            self.conn.execute('UPDATE album_provider_ids SET is_primary=0,equivalence_digest=?,verified_at=? '
                'WHERE album_id=? AND is_primary=1 AND provider IN '
                '(SELECT provider FROM album_provider_ids WHERE album_id=?)', (digest, now, retired, target))
            self.conn.execute('UPDATE album_provider_ids SET album_id=? WHERE album_id=?', (target, retired))
            if typed:
                album_tracklists.merge(self.conn, target, retired)
            self.conn.execute('UPDATE album_alias_reconciliations SET album_id=? WHERE album_id=?', (target, retired))
            self.conn.execute('INSERT INTO album_alias_reconciliations VALUES (?,?,?,?)', (retired, target, digest, now))
