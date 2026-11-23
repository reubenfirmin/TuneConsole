"""Bounded discovery acquisition, selection and retention commands."""
import random

from yt_playlist.core.activity import check_stopping, WorkClosing
from yt_playlist.rec import discovery_pool


def bridge_values(anchors, neighbors, labels, owned):
    bridges = {}
    for name, weight, vector in anchors:
        for candidate in neighbors.get(name, ()):
            if candidate.key not in owned:
                bridges.setdefault(candidate.key, []).append((vector, weight * candidate.score, labels[name]))
    return bridges


def album_catalog(albums, identities, owned, saved):
    return [{'browse_id': row['browse_id'], 'title': row['title'].strip(),
             'year': row.get('year'), 'thumbnail': row.get('thumbnail')}
            for row in albums if row.get('browse_id') and (row.get('title') or '').strip()
            and row['browse_id'] not in saved and identities.get(row['browse_id']) not in owned]


def album_removals(albums, keep, per_artist, rng):
    grouped = {}
    for row in albums:
        grouped.setdefault(row['artist_id'], {})[row['browse_id']] = row.get('offered_count') or 0
    removed = []
    for artist, pooled in grouped.items():
        retained = discovery_pool.choose_album_keep(pooled, per_artist, rng) if artist in keep else set()
        removed.extend(key for key in pooled if key not in retained)
    return removed


class DiscoveryArtists:
    def __init__(self, *, taste, vectors, anchors, owned, reference, cache, similar, neighbors,
                 rank, thumbnail, genre, now, youtube=lambda reference: ()):
        self.taste, self.vectors, self.anchors, self.owned = taste, vectors, anchors, owned
        self.reference, self.cache, self.similar, self.neighbors = reference, cache, similar, neighbors
        self.rank, self.thumbnail, self.genre, self.now = rank, thumbnail, genre, now
        self.youtube = youtube

    def query(self, limit=15, max_anchors=30):
        taste = self.taste()
        _keys, vectors, index = self.vectors()
        anchors = self.anchors(vectors, index, max_anchors)
        if not anchors:
            return []
        owned, now = self.owned(), self.now()
        providers, sources, labels, neighbors = {}, {}, {}, {}
        for name, _weight, _vector in anchors:
            check_stopping()
            reference = self.reference(name)
            label = labels[name] = reference['name'] if reference else name
            if self.similar is not None and self.cache.cached_similar(name, now) is None:
                source_name = self.cache.lookup_name(name)
                if source_name is not None:
                    result, successful = self.similar(source_name)
                    if successful:
                        self.cache.cache_similar(name, result, now, source_name=source_name)
            hits = self.neighbors(name, topn=50, youtube=self.youtube(reference))
            neighbors[name] = hits
            for hit in hits:
                if hit.provider is not None:
                    providers.setdefault(hit.key, hit.provider)
                sources.setdefault(hit.key, {})[name] = hit.sources
        ranked = self.rank(bridge_values(anchors, neighbors, labels, owned), taste,
                           vectors.shape[1] if vectors is not None else 0)
        result = []
        for row in ranked[:limit]:
            check_stopping()
            reference = self.reference(row['cand'])
            provider = providers.get(row['cand'], {})
            artist = reference['name'] if reference else provider.get('name', row['cand'].title())
            result.append({'artist': artist, 'artist_id': reference['id'] if reference else None,
                           'browse_id': provider.get('browse_id'), 'sources': sources[row['cand']],
                           'score': row['score'], 'because': row['because'], 'fits': row['fits'],
                           'thumbnail': provider.get('thumbnail') or self.thumbnail(artist), 'genre': self.genre(artist)})
        return result


class DiscoveryPreparation:
    def __init__(self, query, collection, artists, discovery, eligibility, history, *, parameter,
                 artist_info, new_artists, populate, radio, logger):
        self.query, self.collection, self.artists, self.discovery = query, collection, artists, discovery
        self.eligibility, self.history, self.parameter = eligibility, history, parameter
        self.artist_info, self.new_artists, self.populate, self.radio = artist_info, new_artists, populate, radio
        self.logger = logger

    def scan(self, artist, info, owned, saved, genre, now, per, rng, *, artist_id=None):
        albums = (info or {}).get('albums') or []
        identities = {row['browse_id']: self.collection.album_id(row['browse_id'])
                      for row in albums if row.get('browse_id') and row['browse_id'] not in saved
                      and (row.get('title') or '').strip()}
        catalog = album_catalog(albums, identities, owned, saved)
        pooled = self.discovery.discovered_albums_for_artist(artist_id if artist_id is not None else artist)
        keep, add = discovery_pool.rotate_album_sample(catalog, pooled, per, rng)
        self.discovery.delete_discovered_albums([key for key in pooled if key not in keep])
        for row in add:
            self.discovery.upsert_discovered_album(row['browse_id'], artist, row['title'], row['year'],
                row['thumbnail'], now, genre=genre, artist_id=artist_id)

    def enforce(self, keep_artists, per_artist, rng):
        keep = {self.artists.resolve(artist) for artist in keep_artists}
        return self.discovery.delete_discovered_albums(album_removals(
            self.discovery.get_discovered_albums(), keep, per_artist, rng))

    def cleanup(self, rng=None):
        keep = {row['artist_id'] for row in self.discovery.interested_artists(
            limit=self.parameter('discovery_artist_limit'))}
        removed = self.enforce(keep, self.parameter('discovery_albums_per_artist'), rng or random.Random(0))
        tracks = self.discovery.prune_orphan_discovered_tracks(held_keys=self.eligibility.generated_track_keys())
        return {'albums_removed': removed, 'tracks_removed': tracks}

    def run(self, now, budget=25):
        owned, saved = self.query.owned_albums(), self.collection.saved_album_ids()
        genres = self.query.artist_genres(canonical=True)
        limit, per = self.parameter('discovery_artist_limit'), self.parameter('discovery_albums_per_artist')
        rng = random.Random(int(now))
        due = self.discovery.artists_due_for_scan(now, budget=budget, artist_limit=limit, with_ids=True)
        for reference in due:
            check_stopping()
            try:
                info = self.artist_info(reference['artist'], reference.get('browse_id'))
            except WorkClosing:
                raise
            except Exception:
                info = None
            self.scan(reference['artist'], info, owned, saved, genres.get(self.artists.key(reference['artist_id'])),
                      now, per, rng, artist_id=reference['artist_id'])
            self.discovery.mark_scanned(reference['artist_id'], now)
        spotify = self.history.spotify_history_stats()
        source = 'spotify_model' if any(spotify.get(key) for key in ('events', 'days', 'artists')) else 'discovery'
        for row in self.new_artists():
            artist_id = row.get('artist_id')
            if artist_id is None and row.get('browse_id'):
                artist_id = self.artists.ensure_artist(row['artist'], provider='youtube', provider_id=row['browse_id'])
            self.discovery.upsert_discovered_artist(row['artist'], row['score'], row.get('because'), row.get('fits'),
                row.get('thumbnail'), now, genre=row.get('genre'), artist_id=artist_id, source=source,
                sources=row.get('sources'))
        self.discovery.prune_discovered(owned, saved, self.query.library_artists())
        self.discovery.prune_discovered_tracks(self.query.library_keys(), held_keys=self.eligibility.generated_track_keys())
        self.cleanup(rng)
        self.populate(now)
        try:
            self.radio(now)
        except WorkClosing:
            raise
        except Exception:
            self.logger.info('radio pool population failed (non-fatal)')
        return {'scanned': len(due)}
