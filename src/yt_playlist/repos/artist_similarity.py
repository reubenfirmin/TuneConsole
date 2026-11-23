"""Last.fm artist-neighbour cache and canonical similarity edges."""
import json

from yt_playlist.repos.base import Repo, synchronized


class ArtistSimilarityRepo(Repo):
    @synchronized
    def cached_similar(self, artist_id, *, fetched_since):
        """Return evidence meeting the caller's explicit freshness bound."""
        row = self.conn.execute('SELECT lastfm_similar_json FROM artists WHERE id=? AND lastfm_fetched_at>=?',
                                (artist_id, fetched_since)).fetchone()
        return json.loads(row[0]) if row is not None else None

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
            changed = self.conn.execute('UPDATE artists SET lastfm_similar_json=?,lastfm_fetched_at=? WHERE id=?',
                                        (json.dumps(pairs), now, artist_id)).rowcount
            if changed != 1:
                raise ValueError('Unknown artist for similarity cache')
            self.conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=?', (artist_id,))
            self.conn.executemany('INSERT OR REPLACE INTO artist_similarity_targets VALUES (?,?,?)',
                                  [(artist_id, target, weight) for target, weight in edges])

