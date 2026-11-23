"""Persist reconciled artist evidence in the identity repository's transaction."""
from yt_playlist.core.artist_reconciliation import move_lastfm_cache
from yt_playlist.core.recommendation_invalidation import artist_reconciled
from yt_playlist.repos.base import Repo, snapshot_read, synchronized


class ArtistStateRepo(Repo):
    @snapshot_read
    def reconciliation_inputs(self, previous, target):
        return [dict(row) for row in self.conn.execute(
            'SELECT * FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?', (previous, previous))]

    @synchronized
    def apply_reconciliation(self, previous, target, edges):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE rec_feedback SET artist_id=? WHERE artist_id=?', (target, previous))
            move_lastfm_cache(self.conn, previous, target)
            self.conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                              (previous, previous))
            self.conn.executemany('INSERT INTO artist_similarity_targets VALUES (?,?,?) '
                'ON CONFLICT(source_artist_id,target_artist_id) DO UPDATE SET weight=MAX(weight,excluded.weight)', edges)
            artist_reconciled(self.conn)
