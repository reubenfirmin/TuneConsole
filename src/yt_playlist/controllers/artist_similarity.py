"""Artist similarity cache freshness over stored provider evidence."""
from yt_playlist.core.artist_policy import is_reference, token, unique
from yt_playlist.util.matching import normalize


class ArtistSimilarity:
    def __init__(self, repository, artists):
        self.repository = repository
        self.artists = artists

    def cached_similar(self, artist, now, ttl_days=14):
        return self.repository.cached_similar(self.artists.resolve(artist), fetched_since=now - ttl_days * 86400)

    def artist_similar_edges(self, artist):
        identity = self.artists.resolve(artist)
        return self._edges_by_identity([identity]).get(identity, [])

    def similar_edges_by_artist(self, artists):
        """Resolve the cached Last.fm evidence for a batch of ensemble seeds."""
        identities = self.artists.resolve_many(artists)
        edges = self._edges_by_identity(identities.values())
        return {artist: edges[identity] for artist, identity in identities.items() if identity in edges}

    def _edges_by_identity(self, identities):
        facts = {}
        for identity in dict.fromkeys(identities):
            if identity is None:
                continue
            edges = self.repository.artist_similar_edges(identity)
            source = self.artists.get_artist(identity) if edges else None
            if source is not None:
                pairs = self.repository.cached_similar(identity, fetched_since=float('-inf')) or ()
                facts[identity] = (source['name'], pairs, edges)
        if not facts:
            return {}
        targets = {name for _, pairs, _ in facts.values() for name, _ in pairs}
        explicit = self.artists.resolve_many(name for name in targets if is_reference(name))
        labels = {name for name in targets if not is_reference(name)}
        matches = self.artists.name_matches(labels | {source for source, _, _ in facts.values()})
        result = {}
        for identity, (source, pairs, edges) in facts.items():
            if matches[source] != {identity}:
                continue
            eligible = {}
            for name, weight in pairs:
                target = explicit.get(name) if is_reference(name) else unique(matches[name])
                if target is not None:
                    eligible[target] = max(weight, eligible.get(target, 0.))
            result[identity] = [(token(target), min(weight, eligible[target]))
                                for target, weight in edges if target in eligible]
        return result

    def lookup_name(self, artist):
        """Admit a name-only provider query when the name identifies its seed."""
        reference = self.artists.get_artist(artist)
        if reference is not None:
            name = reference['name']
            if self.artists.name_matches([name])[name] == {reference['id']}:
                return name
        return None

    def cache_similar(self, artist, pairs, now, *, source_name=None):
        pairs = list(pairs)
        identity = self.artists.state_id(artist)
        if source_name is not None and self.lookup_name(identity) != source_name:
            return
        matches = self.artists.name_matches(name for name, _ in pairs)
        edges, unlisted = {}, {}
        for name, weight in pairs:
            if len(matches[name]) > 1:
                continue
            target = next(iter(matches[name]), None)
            if target is None:
                label = normalize(name)
                if label not in unlisted:
                    unlisted[label] = self.artists.state_id(name)
                target = unlisted[label]
            if target != identity:
                edges[target] = max(weight, edges.get(target, 0.))
        return self.repository.cache_similar(identity, pairs, list(edges.items()), now)
