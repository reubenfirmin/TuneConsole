"""Catalog and preparation facts used by onboarding and the account journey."""
from yt_playlist.core import recording_library
from yt_playlist.core.audio_vibe_contract import score_version_filter
from yt_playlist.core.catalog_query import (
    LIBRARY_TRACKS_SQL, PLAYABLE_LIBRARY_SQL, playable_tracks_sql,
)
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read
from yt_playlist.repos.enrichment_sql import pending_coverage

_SCOPE = f'WITH scope AS ({recording_library.ENRICHMENT_SQL}) '


class ReadinessRepo(Repo):
    @snapshot_read
    def familiar_albums(self, *, minimum_songs, minimum_plays):
        """Known releases ordered by distinct owned songs and listening evidence."""
        return [dict(row) for row in self.conn.execute(
            "WITH familiar AS (SELECT t.artist,COUNT(DISTINCT t.identity_key) songs,"
            f"SUM(COALESCE(d.plays,0)) plays FROM ({PLAYABLE_LIBRARY_SQL}) t "
            "LEFT JOIN listening_day_counts d ON d.identity_key=t.identity_key GROUP BY t.artist) "
            "SELECT p.provider_id browse,a.title,a.artist,a.year,a.type,a.thumbnail,f.songs,f.plays "
            "FROM albums a JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' AND p.is_primary=1 "
            "JOIN familiar f ON f.artist=a.artist COLLATE NOCASE "
            "ORDER BY (f.songs>=? OR f.plays>=?) DESC,f.plays DESC,f.songs DESC,a.title COLLATE NOCASE",
            (minimum_songs, minimum_plays))]

    @snapshot_read
    def arrival(self):
        owned = playable_tracks_sql(recording_library.OWNED_TRACKS_SQL)
        songs = self.conn.execute(f'SELECT COUNT(*) FROM ({owned})').fetchone()[0]
        covers = [dict(row) for row in self.conn.execute(
            f'SELECT video_id,thumbnail FROM ({owned}) ORDER BY identity_key LIMIT 6')]
        return {'songs': songs, 'covers': covers}

    @snapshot_read
    def library_sample(self, per_genre, *, maximum_duration):
        """Bound each genre bucket to the number the caller can display."""
        eligible = f'SELECT * FROM ({LIBRARY_TRACKS_SQL}) WHERE duration_s IS NULL OR duration_s<=?'
        return [dict(row) for row in self.conn.execute(
            'WITH sampled AS (SELECT identity_key k,title,artist,album,video_id vid,thumbnail thumb,'
            'genre,duration_s duration,row_number() OVER (PARTITION BY genre ORDER BY random()) AS position '
            f'FROM ({playable_tracks_sql(eligible)})) '
            'SELECT k,title,artist,album,vid,thumb,genre,duration FROM sampled WHERE position<=?',
            (maximum_duration, per_genre))]

    @lookup_read
    def playable_owned(self, *, exclude_generated, maximum_duration):
        library = recording_library.OWNED_TRACKS_SQL
        quarantine = (
            ' AND (NOT EXISTS(SELECT 1 FROM playlist_tracks pt JOIN active_playlists p ON p.id=pt.playlist_id '
            'JOIN quarantined_playlists q ON q.playlist_scope_id=p.playlist_scope_id WHERE pt.track_id=t.id) '
            'OR EXISTS(SELECT 1 FROM playlist_tracks pt JOIN active_playlists p ON p.id=pt.playlist_id '
            'LEFT JOIN quarantined_playlists q ON q.playlist_scope_id=p.playlist_scope_id '
            'WHERE pt.track_id=t.id AND q.playlist_scope_id IS NULL))' if exclude_generated else '')
        return self.conn.execute(f'SELECT 1 FROM ({library}) t WHERE t.is_playable AND '
            '(t.duration_s IS NULL OR t.duration_s<=?)' + quarantine + ' LIMIT 1',
            (maximum_duration,)).fetchone() is not None

    @lookup_read
    def live_starter_played(self, videos):
        if not videos:
            return False
        return self.conn.execute('SELECT 1 FROM play_events e JOIN play_event_sources s ON s.event_id=e.id '
            "WHERE s.source='live' AND e.is_generated=1 "
            f"AND e.video_id IN ({','.join('?' for _ in videos)}) LIMIT 1", videos).fetchone() is not None

    @snapshot_read
    def coverage(self):
        return tuple(self.conn.execute(_SCOPE +
            'SELECT COUNT(*),COALESCE(SUM(r.first_enriched_at IS NOT NULL AND NOT ' +
            pending_coverage('r.id') + '),0) FROM scope s JOIN recordings r ON r.id=s.recording_id').fetchone())

    @lookup_read
    def has_vibe_evidence(self, patterns, *, sources, minimum_support):
        return self.conn.execute(_SCOPE +
            'SELECT 1 FROM served_vibe_fits v JOIN scope s ON s.recording_id=v.recording_id '
            f"WHERE v.support>? AND v.source IN ({','.join('?' for _ in sources)}) "
            'AND ' + score_version_filter('v.') + ' LIMIT 1',
            (minimum_support, *sources, *patterns)).fetchone() is not None

    @snapshot_read
    def vibe_evidence(self, patterns, *, measured_sources, proxy_sources, minimum_support):
        return tuple(self.conn.execute(_SCOPE +
            f"SELECT COUNT(DISTINCT CASE WHEN v.source IN ({','.join('?' for _ in measured_sources)}) "
            f"THEN v.recording_id END), COUNT(DISTINCT CASE WHEN v.source IN ({','.join('?' for _ in proxy_sources)}) "
            "THEN v.recording_id END) FROM served_vibe_fits v JOIN scope s ON s.recording_id=v.recording_id "
            'WHERE v.support>? AND ' + score_version_filter('v.'),
            (*measured_sources, *proxy_sources, minimum_support, *patterns)).fetchone())

    @snapshot_read
    def journey_history(self, *, provider, completed_statuses):
        first_live_at = self.conn.execute(
            'SELECT MIN(e.played_at) FROM play_events e JOIN play_event_sources s ON s.event_id=e.id '
            "WHERE s.source='live'").fetchone()[0]
        takeout = self.conn.execute(
            'SELECT COUNT(DISTINCT e.id) FROM play_events e JOIN play_event_sources s ON s.event_id=e.id '
            "WHERE s.source='takeout' AND e.played_at IS NOT NULL AND e.video_id IS NOT NULL").fetchone()[0]
        config = self.conn.execute(
            'SELECT status,fingerprint FROM enrichment_provider_config WHERE provider=? AND status IS NOT NULL', (provider,)).fetchone()
        checked = self.conn.execute(_SCOPE +
            'SELECT COUNT(*) FROM scope s JOIN enrichment_provider_state p ON p.recording_id=s.recording_id '
            f"WHERE p.provider=? AND p.status IN ({','.join('?' for _ in completed_statuses)}) "
            'AND p.queued=0', (provider, *completed_statuses)).fetchone()[0]
        return {'live_starts': first_live_at is not None, 'first_live_at': first_live_at,
            'takeout': takeout,
            'takeout_started': self.conn.execute('SELECT 1 FROM takeout_imports LIMIT 1').fetchone() is not None,
            'lastfm_config': tuple(config) if config else None, 'lastfm_checked': checked,
            'created_playlists': self.conn.execute(
                "SELECT COUNT(DISTINCT playlist_scope_id) FROM active_playlists WHERE created_at_source='local'").fetchone()[0]}
