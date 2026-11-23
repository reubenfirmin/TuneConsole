"""Complete analytics queries and commands, independent of HTTP and templates."""
from zoneinfo import ZoneInfo

from yt_playlist.rec.ticker import periods as ticker_periods

DIMENSIONS = ('genre', 'year', 'album', 'playlist', 'artist')


class AnalyticsQueries:
    def __init__(self, *, reader, listening, artists, collection, provider, settings,
                 capture, now, refresh_analytics):
        self.reader, self.listening = reader, listening
        self.artists, self.collection, self.provider = artists, collection, provider
        self.settings, self.capture, self.now = settings, capture, now
        self.refresh_analytics = refresh_analytics

    def prepared(self):
        return {'analytics': self.listening.prepared() or {},
                'edition': self.listening.prepared('listening_edition') or {}}

    def encoded(self, key=None):
        return self.listening.encoded('listening_chunk:' + key if key else 'listening_analytics')

    def timezone(self, name):
        if not isinstance(name, str) or len(name) > 100:
            raise ValueError('Invalid timezone')
        ZoneInfo(name)
        self.settings.set_setting('listening_timezone', name)
        self.refresh_analytics()
        return name

    def impressions(self, edition_id, ids):
        edition = self.listening.prepared('listening_edition') or {}
        if edition_id != edition.get('id'):
            return True
        if not isinstance(ids, list) or len(ids) > 4 or any(not isinstance(s, str) for s in ids):
            return False
        stories = edition.get('stories', [])
        signatures = {s['id'] for s in stories}.intersection(ids)
        signatures.update('family:' + s['family'] for s in stories
                          if s['id'] in signatures and s.get('family'))
        if stories and stories[0]['id'] in signatures:
            signatures.add('hero:' + stories[0]['id'])
        capture = self.capture()
        if signatures and capture is not None:
            capture.enqueue('listening editorial exposure', self.listening.record_impressions,
                            signatures, self.now())
        return True

    @staticmethod
    def ticker(reader, dim, periods):
        return {'corpus': reader.charts.corpus_distribution(dim),
            'windows': {label: reader.charts.listen_distribution(dim, since=lo, until=hi)
                        for label, (lo, hi) in periods},
            'browse': reader.charts.album_browse_ids() if dim == 'album' else {},
            'playlists': {f'@playlist:{p.id}': p.id for p in reader.playlists.get_playlists()}
                         if dim == 'playlist' else {},
            'thumbnails': reader.charts.ticker_thumbnails(dim) if dim in ('artist', 'album', 'playlist') else {},
            'labels': (reader.charts.album_labels() if dim == 'album' else
                       reader.charts.playlist_labels() if dim == 'playlist' else
                       reader.charts.artist_labels() if dim == 'artist' else {})}

    def rankings(self, days):
        now = self.now()
        with self.reader() as reader:
            earliest, _ = reader.charts.history_bounds()
            periods, close_days, span_days = ticker_periods(earliest, now, days)
            tickers = {dim: self.ticker(reader, dim, periods) for dim in DIMENSIONS}
            songs = reader.charts.top_tracks(100, since=None if days is None else now - days * 86400.)
        return {'tickers': tickers, 'songs': songs, 'close_days': close_days, 'span_days': span_days}

    def artist(self, name, browse_id=None, artist_id=None):
        # Explicit IDs take precedence over stale display names. A bad ID must
        # never fall through to another artist with the same name.
        if artist_id is not None:
            canonical = self.artists.get_artist(f'@artist:{artist_id}')
            if canonical is None:
                return None
        elif browse_id:
            identity = self.artists.provider_identity(browse_id)
            canonical = self.artists.get_artist(f'@artist:{identity}') if identity is not None else None
        else:
            identity = self.artists.resolve_exact(name) if name else None
            canonical = self.artists.get_artist(f'@artist:{identity}') if identity is not None else None
            if canonical is None:
                return None
        if canonical:
            name = canonical['name']
            browse_id = canonical['provider_ids'].get('youtube')
        info = self.provider(name, browse_id) if browse_id else None
        if not canonical:
            name = (info or {}).get('name') or name
            if not name:
                return None
        with self.reader() as reader:
            songs = reader.charts.artist_songs(canonical['id'], None) if canonical else []
            saved_ids = reader.collection.saved_album_ids()
            collection = reader.collection.artist_albums(canonical['id'], None) if canonical else []
            owners = reader.collection.saved_album_owners()
            identities = reader.collection.canonical_album_ids(
                [a.get('browse') for a in collection] +
                [a.get('browse_id') for a in (info or {}).get('albums', [])])
        return {'artist': name, 'info': info, 'songs': songs, 'collection': collection,
                'saved_ids': saved_ids, 'saved_owners': owners, 'identities': identities}

class StoryQueries:
    def __init__(self, *, proposals, settings, refresh, paused):
        self.proposals, self.settings = proposals, settings
        self.refresh, self.paused = refresh, paused

    def story(self, month):
        story = (self.proposals.get_proposals('trend_rollups') or {}).get('story')
        if not story or story.get('month') != month:
            return None
        if any('groups' in beat or (beat['kind'] == 'personality' and 'audio_plays' not in beat)
               for beat in story['beats']):
            self.refresh()
        return story

    def viewed(self, month):
        if not self.paused():
            self.settings.set_setting('recap_viewed_month', month)
