"""Account-scoped YouTube Music operations for the application."""
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.matching import track_artist
from yt_playlist.util.thumbnails import best_thumb


def _tracks(data, *, album_artist=None, include_artwork=True):
    credits = data.get('artists')
    fallback = album_artist or ', '.join(row['name'] for row in credits or [] if row.get('name'))
    for row in data.get('tracks') or []:
        video, title = row.get('videoId'), (row.get('title') or '').strip()
        if video and title:
            yield dict(video=video, title=title, artist=track_artist(row) or fallback,
                       thumbnail=best_thumb(row.get('thumbnail') or row.get('thumbnails')) if include_artwork else None,
                       credits=row.get('artists') or credits)


class YouTubeMusicAccess:
    """Expose provider operations without exposing the SDK or its transport."""

    def __init__(self, provider):
        self._provider = provider

    @classmethod
    def for_client(cls, client):
        if client is None or isinstance(client, cls):
            return client
        return cls(client)

    def search(self, query, filter=None, **options):
        return self._provider.search(query, filter=filter, **options)

    def search_artists(self, name):
        return self.search(name, filter='artists') or []

    def artist_thumbnail(self, name):
        rows = self.search_artists(name)
        return best_thumb(rows[0].get('thumbnails')) if rows else None

    def artist_artwork(self, name, *, limit=5):
        return [dict(artist=row.get('artist'), title=row.get('title'), browse_id=row.get('browseId'),
                     thumbnail=best_thumb(row.get('thumbnails')))
                for row in self.search_artists(name)[:limit]
                if row.get('resultType') in (None, 'artist')]

    def get_artist(self, browse_id):
        return self._provider.get_artist(browse_id)

    def artist_profile(self, browse_id):
        data = self.get_artist(browse_id)
        releases = (data.get('albums') or {}).get('results') or []
        return dict(bio=data.get('description'), thumbnail=best_thumb(data.get('thumbnails')),
                    subscribers=data.get('subscribers'), name=data.get('name'), browse_id=browse_id,
                    albums=[dict(title=row.get('title'), year=row.get('year'), browse_id=row.get('browseId'),
                                 thumbnail=best_thumb(row.get('thumbnails'))) for row in releases])

    def get_album(self, browse_id):
        return self._provider.get_album(browse_id)

    @property
    def can_read_albums(self):
        return callable(getattr(self._provider, 'get_album', None))

    def album_tracks(self, browse_id, *, album_artist=None):
        data = self.get_album(browse_id) or {}
        return bool(data.get('tracks')), _tracks(data, album_artist=album_artist, include_artwork=False)

    def radio_tracks(self, video_id):
        data = self._provider.get_watch_playlist(video_id) or {}
        return _tracks(data)

    def home_tracks(self):
        """Return playable rows from the account's Home shelves in provider order."""
        tracks = []
        for shelf in self._provider.get_home() or []:
            contents = shelf.get('contents')
            if not isinstance(contents, list):
                continue
            for row in contents:
                if not isinstance(row, dict) or not row.get('videoId'):
                    continue
                artists = row.get('artists') or []
                tracks.append(dict(video_id=row['videoId'], title=row.get('title') or '',
                                   artist=(artists[0].get('name') or '') if artists else '',
                                   duration=row.get('duration_seconds') or parse_duration(row.get('duration'))))
        return tracks

    def get_song(self, video_id, **options):
        return self._provider.get_song(video_id, **options)

    def get_video_storyboard(self, video_id):
        return self._provider.get_video_storyboard(video_id)

    def get_account_info(self):
        return self._provider.get_account_info()

    def get_history(self):
        return self._provider.get_history()

    @property
    def history_timezone(self):
        return self._provider.history_timezone

    def get_library_songs(self, limit=25, **options):
        return self._provider.get_library_songs(limit=limit, **options)

    def get_library_playlists(self, limit=25):
        return self._provider.get_library_playlists(limit=limit)

    def get_library_albums(self, limit=25, order=None):
        return self._provider.get_library_albums(limit=limit, order=order)

    def get_library_upload_album(self, browse_id):
        return self._provider.get_library_upload_album(browse_id)

    def get_playlist(self, playlist_id, limit=100, **options):
        return self._provider.get_playlist(playlist_id, limit=limit, **options)

    def playlist_read_report(self, playlist_id):
        from yt_playlist.core.playlist_snapshot import playlist_read_report
        return playlist_read_report(self._provider, playlist_id)

    def deletion_read_report(self, playlist_id, expected_brand):
        return self._provider.deletion_read_report(playlist_id, expected_brand)

    def create_playlist(self, title, description, **options):
        return self._provider.create_playlist(title, description, **options)

    def edit_playlist(self, playlist_id, **changes):
        return self._provider.edit_playlist(playlist_id, **changes)

    def delete_playlist(self, playlist_id):
        return self._provider.delete_playlist(playlist_id)

    def add_playlist_items(self, playlist_id, video_ids, **options):
        return self._provider.add_playlist_items(playlist_id, video_ids, **options)

    def remove_playlist_items(self, playlist_id, tracks):
        return self._provider.remove_playlist_items(playlist_id, tracks)

    def rate_song(self, video_id, rating):
        return self._provider.rate_song(video_id, rating)

    def rate_playlist(self, playlist_id, rating):
        return self._provider.rate_playlist(playlist_id, rating)

    def subscribe_artist(self, channel_id):
        return self._provider.subscribe_artist(channel_id)

    def edit_song_library_status(self, feedback_tokens):
        return self._provider.edit_song_library_status(feedback_tokens)
