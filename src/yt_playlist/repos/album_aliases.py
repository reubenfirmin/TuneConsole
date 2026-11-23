"""Database facts and collection-row changes for verified album aliases."""
from yt_playlist.core import album_memberships, album_tracklists
from yt_playlist.core.album_queries import FETCH_STATE
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class AlbumAliasesRepo(Repo):
    @lookup_read
    def fingerprint_inputs(self, browse):
        row = self.conn.execute('SELECT p.album_id,p.fingerprint_digest AS digest FROM album_provider_ids p '
            "WHERE p.provider='youtube' AND p.provider_id=?", (browse,)).fetchone()
        shared = row is not None and self.conn.execute('SELECT 1 FROM album_provider_ids '
            "WHERE provider='youtube' AND album_id=? AND provider_id<>? LIMIT 1",
            (row['album_id'], browse)).fetchone() is not None
        return {'shared': shared, 'digest': row['digest'] if row else None}

    @synchronized
    def remember_fingerprint(self, browse, album_id, digest, now):
        with self.conn.atomic():
            self.conn.execute('UPDATE album_provider_ids SET fingerprint_digest=?,fingerprint_at=? '
                "WHERE provider='youtube' AND provider_id=?", (digest, now, browse))
            return [row[0] for row in self.conn.execute('SELECT DISTINCT album_id FROM album_provider_ids '
                "WHERE provider='youtube' AND fingerprint_digest=? AND album_id<>? ORDER BY album_id", (digest, album_id))]

    def _track_inputs(self, album_id):
        revisions = []
        for row in self.conn.execute('SELECT id,status,order_status FROM album_tracklists WHERE album_id=? '
                "AND capture_kind IN ('provider','legacy_full')", (album_id,)):
            revisions.append({**dict(row), 'occurrences': [dict(item) for item in self.conn.execute(
                'SELECT o.position,coalesce(e.video_id,o.raw_source_ref) video_id '
                'FROM album_track_occurrences o LEFT JOIN album_occurrence_sources s ON s.occurrence_id=o.id '
                'LEFT JOIN track_sources e ON e.id=s.entry_id WHERE o.tracklist_id=? ORDER BY o.position,o.id',
                (row['id'],))]})
        tracks = [dict(row) for row in self.conn.execute('SELECT t.*,e.video_id FROM album_tracks t '
            'LEFT JOIN track_sources e ON e.id=t.entry_id WHERE t.album_id=? '
            'ORDER BY t.position IS NULL,t.position,t.id', (album_id,))]
        state = self.conn.execute(f'SELECT complete,expected_track_count FROM {FETCH_STATE} WHERE album_id=?',
                                  (album_id,)).fetchone()
        return {'revisions': revisions, 'tracks': tracks, 'state': dict(state) if state else None}

    @snapshot_read
    def merge_inputs(self, target, retired):
        conflict = self.conn.execute('SELECT 1 FROM album_provider_ids a JOIN album_provider_ids b '
            "ON a.provider=b.provider AND a.provider<>'youtube' AND a.provider_id<>b.provider_id "
            'WHERE a.album_id=? AND b.album_id=? LIMIT 1', (target, retired)).fetchone() is not None
        return {'conflict': conflict, 'target': self._track_inputs(target),
                'retired': self._track_inputs(retired)}

    @synchronized
    def apply_merge(self, target, retired, digest, now):
        with self.conn.atomic():
            album_memberships.merge(self.conn, target, retired)
            self.conn.execute('UPDATE album_provider_ids SET is_primary=0,equivalence_digest=?,verified_at=? '
                'WHERE album_id=? AND is_primary=1 AND provider IN '
                '(SELECT provider FROM album_provider_ids WHERE album_id=?)', (digest, now, retired, target))
            self.conn.execute('UPDATE album_provider_ids SET album_id=? WHERE album_id=?', (target, retired))
            album_tracklists.merge(self.conn, target, retired)
            self.conn.execute('UPDATE albums SET canonical_album_id=? WHERE canonical_album_id=?', (target, retired))
            self.conn.execute('UPDATE albums SET canonical_album_id=?,merge_digest=?,merged_at=? WHERE id=?',
                              (target, digest, now, retired))
