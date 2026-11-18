"""Bounded provider catalog commands; selection uses detached provider responses."""
from yt_playlist.core.activity import check_stopping, WorkClosing
from yt_playlist.core.album_aliases import _text, fingerprint
from yt_playlist.util.matching import identity_key, track_artist
from yt_playlist.util.thumbnails import best_thumb


def alias_pairs(artist, discography, candidates):
    for remote in discography:
        bid = remote.get('browse_id') or ''
        if not bid.startswith('MPRE'):
            continue
        for local in candidates:
            other = local['browse_id']
            if (other != bid and other.startswith('MPRE') and
                    _text(local['artist']) == _text(artist) and
                    _text(local['title']) == _text(remote.get('title'))):
                yield bid, other


def track_values(data, album=None):
    album = album or {}
    artist = album.get('artist') or ', '.join(
        row['name'] for row in (data.get('artists') or []) if row.get('name'))
    for row in data.get('tracks') or []:
        video, title = row.get('videoId'), (row.get('title') or '').strip()
        if video and title:
            credit = track_artist(row) or artist
            yield dict(identity=identity_key(title, credit), video=video, title=title,
                artist=credit, album=album.get('title'),
                thumbnail=album.get('thumbnail') if album else best_thumb(row.get('thumbnail') or row.get('thumbnails')),
                genre=album.get('genre'), year=album.get('year'),
                credits=row.get('artists') or data.get('artists'))


class DiscoveryCatalog:
    def __init__(self, *, catalog, radio_client, collection, discovery, query, tracks, charts,
                 settings, rebuild, now, logger):
        self.catalog, self.radio_client, self.collection = catalog, radio_client, collection
        self.discovery, self.query, self.tracks = discovery, query, tracks
        self.charts, self.settings, self.rebuild = charts, settings, rebuild
        self.now, self.logger = now, logger

    def aliases(self, client, artist, albums):
        if not hasattr(client, 'get_album'):
            return
        candidates = self.collection.album_alias_candidates(row.get('title') for row in albums)
        for bid, other in alias_pairs(artist, albums, candidates):
            if not self.collection.claim_album_alias_check(bid, other, self.now()):
                continue
            try:
                check_stopping()
                left = client.get_album(other)
                check_stopping()
                right = client.get_album(bid)
                stamp = fingerprint(left)
                if stamp is not None and stamp == fingerprint(right):
                    self.collection.observe_album_representation(other, left)
                    self.collection.observe_album_representation(bid, right)
            except WorkClosing:
                raise
            except Exception:
                self.logger.debug('Album alias comparison unavailable for %s and %s', other, bid, exc_info=True)
            return

    def artist(self, name, browse_id=None):
        check_stopping()
        if not browse_id:
            return None
        try:
            client = self.catalog()
            if client is None:
                return None
            data = client.get_artist(browse_id)
            albums = [dict(title=row.get('title'), year=row.get('year'), browse_id=row.get('browseId'),
                thumbnail=best_thumb(row.get('thumbnails'))) for row in (data.get('albums') or {}).get('results') or []]
            self.aliases(client, name, albums)
            return dict(bio=data.get('description'), thumbnail=best_thumb(data.get('thumbnails')),
                subscribers=data.get('subscribers'), name=data.get('name') or name, browse_id=browse_id, albums=albums)
        except WorkClosing:
            raise
        except Exception:
            self.logger.info('artist info fetch failed for %r (non-fatal)', name)
            return None

    def _put(self, row, source, now):
        return self.discovery.upsert_discovered_track(row['identity'], row['video'], row['title'], row['artist'],
            row['album'], row['thumbnail'], row['genre'], row['year'], source, now, credits=row['credits'])

    def populate(self, now, budget=4):
        client = self.catalog()
        if client is None:
            return 0
        owned, have = self.query.library_keys(), self.discovery.discovered_source_tokens('album')
        albums = self.discovery.get_discovered_albums()
        fetched = self.discovery.fetched_albums(row['browse_id'] for row in albums)
        todo = [row for row in albums if row['browse_id'] not in have | fetched][:budget]
        count = 0
        for album in todo:
            check_stopping()
            try:
                data = client.get_album(album['browse_id']) or {}
            except WorkClosing:
                raise
            except Exception:
                continue
            for row in track_values(data, album):
                if self.tracks.identity_key_for_video(row['video']) not in owned:
                    self._put(row, album['browse_id'], now)
                    count += 1
            if data.get('tracks'):
                self.discovery.remember_album_fetch(album['browse_id'])
        if count:
            self.rebuild()
        return count

    def radio(self, now, seeds=6, per_seed_limit=12):
        if self.settings.get_setting('radio_discovery_enabled', '0') != '1':
            return 0
        client = self.radio_client()
        if client is None:
            return 0
        owned, have = self.query.library_keys(), self.discovery.discovered_track_keys()
        count = 0
        for seed in self.charts.top_tracks(seeds):
            if not (video := seed.get('video_id')):
                continue
            check_stopping()
            try:
                data = client.get_watch_playlist(video) or {}
            except WorkClosing:
                raise
            except Exception:
                continue
            taken = 0
            for row in track_values(data):
                key = self.tracks.identity_key_for_video(row['video'])
                if key in owned:
                    continue
                existing = key in have
                key = self._put(row, f'radio:{video}', now)
                have.add(key)
                count += int(not existing)
                taken += 1
                if taken >= per_seed_limit:
                    break
        return count
