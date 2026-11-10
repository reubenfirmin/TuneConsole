"""Artist similarity cache freshness over stored provider evidence."""
from yt_playlist.core.artists import token


class ArtistSimilarity:
    def __init__(self, repository, artists):
        self.repository = repository
        self.artists = artists

    def cached_similar(self, artist, now, ttl_days=14):
        return self.repository.cached_similar(self.artists.resolve(artist), fetched_since=now - ttl_days * 86400)

    def artist_similar_edges(self, artist):
        return [(token(identity), weight) for identity, weight in
                self.repository.artist_similar_edges(self.artists.resolve(artist))]

    def cache_similar(self, artist, pairs, now):
        pairs = list(pairs)
        identity = self.artists.state_id(artist)
        edges = [(self.artists.state_id(name), weight) for name, weight in pairs]
        return self.repository.cache_similar(identity, pairs, edges, now)
