"""Chart presentation and listening policy over detached repository results."""
from yt_playlist.library.playlist_sessions import PLAYLIST_SESSIONS
from yt_playlist.util.matching import recording_title


class Charts:
    def __init__(self, repository):
        self.repository = repository

    def album_labels(self):
        return {key: title or 'Album' for key, title in self.repository.album_labels().items()}

    def playlist_labels(self):
        return {key: title or 'Playlist' for key, title in self.repository.playlist_labels().items()}

    def get_playlist_playback_stats(self):
        return self.repository.get_playlist_playback_stats(policy=PLAYLIST_SESSIONS)

    def story_tracks(self, *, since, artist=None, genres=(), limit=3):
        if limit <= 0 or not (artist or genres):
            return []
        songs, seen = [], set()
        with self.repository.story_track_rows(since=since, artist=artist, genres=genres) as rows:
            for row in rows:
                display = recording_title(row['title']), recording_title(row['artist'])
                if display in seen:
                    continue
                seen.add(display)
                songs.append(row)
                if len(songs) >= limit:
                    break
        return songs

    def ticker_thumbnails(self, dimension):
        return self.repository.ticker_thumbnails(dimension)

    def album_browse_ids(self):
        return self.repository.album_browse_ids()

    def history_bounds(self):
        return self.repository.history_bounds()

    def corpus_distribution(self, dimension):
        return self.repository.corpus_distribution(dimension)

    def listen_distribution(self, dimension, since=None, until=None):
        return self.repository.listen_distribution(dimension, since, until)

    def get_playlist_listen_stats(self):
        return self.repository.get_playlist_listen_stats()

    def get_playlist_track_recency(self, *, include_generated=False, playlist_id=None):
        return self.repository.get_playlist_track_recency(
            include_generated=include_generated, playlist_id=playlist_id)

    def top_tracks(self, limit=100, since=None):
        return self.repository.top_tracks(limit, since)

    def top_artists(self, limit=100, since=None):
        return self.repository.top_artists(limit, since)

    def story_activity(self, since):
        return self.repository.story_activity(since)

    def artist_thumbnail(self, artist):
        return self.repository.artist_thumbnail(artist)

    def genre_representative(self, genres):
        return self.repository.genre_representative(genres)

    def playlist_tracks_detail(self, playlist_id):
        return self.repository.playlist_tracks_detail(playlist_id)

    def video_tracks_detail(self, video_ids):
        return self.repository.video_tracks_detail(video_ids)

    def album_tracks_detail(self, album_browse_id):
        return self.repository.album_tracks_detail(album_browse_id)

    def artist_songs(self, artist, browse_id=None):
        return self.repository.artist_songs(artist, browse_id)
