"""Bounded provider catalog commands; selection uses detached provider responses."""
from yt_playlist.core.activity import check_stopping, WorkClosing
from yt_playlist.core.album_aliases import _text, fingerprint
from yt_playlist.util.matching import identity_key


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


class DiscoveryCatalog:
    def __init__(self, *, catalog, radio_client, collection, discovery, query, tracks, charts,
                 settings, rebuild, now, logger):
        self.catalog, self.radio_client, self.collection = catalog, radio_client, collection
        self.discovery, self.query, self.tracks = discovery, query, tracks
        self.charts, self.settings, self.rebuild = charts, settings, rebuild
        self.now, self.logger = now, logger

    def aliases(self, client, artist, albums):
        if not client.can_read_albums:
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
            profile = client.artist_profile(browse_id)
            if profile['albums']:
                self.aliases(client, name, profile['albums'])
            return {**profile, 'name': profile['name'] or name}
        except WorkClosing:
            raise
        except Exception:
            self.logger.info('artist info fetch failed for %r (non-fatal)', name)
            return None

    def _put(self, row, source, now, *, album=None):
        thumbnail = album.get('thumbnail') if album else row['thumbnail']
        album = album or {}
        return self.discovery.upsert_discovered_track(identity_key(row['title'], row['artist']),
            row['video'], row['title'], row['artist'], album.get('title'), thumbnail, album.get('genre'),
            album.get('year'), source, now, credits=row['credits'])

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
                has_tracks, rows = client.album_tracks(album['browse_id'], album_artist=album.get('artist'))
            except WorkClosing:
                raise
            except Exception:
                continue
            for row in rows:
                if self.tracks.identity_key_for_video(row['video']) not in owned:
                    self._put(row, album['browse_id'], now, album=album)
                    count += 1
            if has_tracks:
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
                rows = client.radio_tracks(video)
            except WorkClosing:
                raise
            except Exception:
                continue
            taken = 0
            for row in rows:
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
