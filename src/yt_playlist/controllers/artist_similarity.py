"""Artist similarity cache freshness over stored provider evidence."""


class ArtistSimilarity:
    def __init__(self, repository):
        self.repository = repository

    def cached_similar(self, artist, now, ttl_days=14):
        return self.repository.cached_similar(artist, fetched_since=now - ttl_days * 86400)

    def artist_similar_edges(self, artist):
        return self.repository.artist_similar_edges(artist)

    def cache_similar(self, artist, pairs, now):
        return self.repository.cache_similar(artist, pairs, now)
