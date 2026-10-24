"""Last.fm artist-neighbour cache and canonical similarity edges."""
import json

from yt_playlist.repos.base import Repo, synchronized


class ArtistSimilarityRepo(Repo):
    @synchronized
    def cached_similar(self, artist, now, ttl_days=14):
        """Cached Last.fm similar-artist list, or None if missing/expired."""
        from yt_playlist.core import artists
        aid = artists.lookup(self.conn,artist)
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM rec_artist_similar WHERE artist_id=?", (aid,)).fetchone()
        if row is None or row["fetched_at"] is None or now - row["fetched_at"] > ttl_days * 86400:
            return None
        return json.loads(row["payload"])

    @synchronized
    def artist_similar_edges(self, artist) -> list:
        """Cached Last.fm similar pairs [[name, match], ...] for an artist, ignoring TTL ([] if none).
        Used by the #28 artist model's §C edge block, where a slightly-stale relatedness hint is fine
        (the model rebuilds periodically and new_artists keeps the cache warm)."""
        from yt_playlist.core import artists
        aid = artists.lookup(self.conn,artist)
        return [(artists.token(r[0]),r[1]) for r in self.conn.execute(
            'SELECT target_artist_id,weight FROM artist_similarity_targets WHERE source_artist_id=?', (aid,))]

    @synchronized
    def cache_similar(self, artist, pairs, now) -> None:
        from yt_playlist.core import artists
        aid = artists.state_id(self.conn,artist)
        self.conn.execute(
            "INSERT INTO rec_artist_similar(artist, payload, fetched_at,artist_id) VALUES (?,?,?,?) "
            "ON CONFLICT(artist_id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (artists.token(aid), json.dumps(pairs), now,aid))
        self.conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=?', (aid,))
        for name,weight in pairs:
            target = artists.state_id(self.conn,name)
            self.conn.execute('INSERT OR REPLACE INTO artist_similarity_targets VALUES (?,?,?)', (aid,target,weight))
        self.conn.commit()

