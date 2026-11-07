"""Last.fm artist-neighbour cache and canonical similarity edges."""
import json

from yt_playlist.repos.base import Repo, synchronized


class ArtistSimilarityRepo(Repo):
    @synchronized
    def cached_similar(self, artist_id, *, fetched_since):
        """Return evidence meeting the caller's explicit freshness bound."""
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM rec_artist_similar WHERE artist_id=?", (artist_id,)).fetchone()
        if row is None or row["fetched_at"] is None or row['fetched_at'] < fetched_since:
            return None
        return json.loads(row["payload"])

    @synchronized
    def artist_similar_edges(self, artist_id) -> list:
        """Cached Last.fm similar pairs [[name, match], ...] for an artist, ignoring TTL ([] if none).
        Used by the #28 artist model's §C edge block, where a slightly-stale relatedness hint is fine
        (the model rebuilds periodically and new_artists keeps the cache warm)."""
        return [tuple(row) for row in self.conn.execute(
            'SELECT target_artist_id,weight FROM artist_similarity_targets WHERE source_artist_id=?', (artist_id,))]

    @synchronized
    def cache_similar(self, artist_id, pairs, edges, now) -> None:
        with self.conn.atomic(join=True):
            self.conn.execute(
                "INSERT INTO rec_artist_similar(artist, payload, fetched_at,artist_id) VALUES (?,?,?,?) "
                "ON CONFLICT(artist_id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
                (f'@artist:{artist_id}', json.dumps(pairs), now, artist_id))
            self.conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=?', (artist_id,))
            self.conn.executemany('INSERT OR REPLACE INTO artist_similarity_targets VALUES (?,?,?)',
                                  [(artist_id, target, weight) for target, weight in edges])

