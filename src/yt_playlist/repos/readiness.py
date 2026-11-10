"""Catalog and preparation facts used by onboarding and the account journey."""
from yt_playlist.core import vibe_versions
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read
from yt_playlist.repos.enrichment_sql import pending_coverage

_SCOPE = ("WITH scope AS (SELECT e.recording_id FROM catalog_entries e "
          "JOIN library_memberships l ON l.entry_id=e.id UNION "
          "SELECT recording_id FROM discovery_memberships) ")


class ReadinessRepo(Repo):
    @snapshot_read
    def familiar_albums(self, *, minimum_songs, minimum_plays):
        """Known releases ordered by distinct owned songs and listening evidence."""
        return [dict(row) for row in self.conn.execute(
            "WITH familiar AS (SELECT t.artist,COUNT(DISTINCT t.identity_key) songs,"
            "SUM(COALESCE(d.plays,0)) plays FROM playable_library_tracks t "
            "LEFT JOIN listening_day_counts d ON d.identity_key=t.identity_key GROUP BY t.artist) "
            "SELECT p.provider_id browse,a.title,a.artist,a.year,a.type,a.thumbnail,f.songs,f.plays "
            "FROM albums a JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            "JOIN familiar f ON f.artist=a.artist COLLATE NOCASE "
            "ORDER BY (f.songs>=? OR f.plays>=?) DESC,f.plays DESC,f.songs DESC,a.title COLLATE NOCASE",
            (minimum_songs, minimum_plays))]

    @snapshot_read
    def arrival(self):
        songs = self.conn.execute('SELECT COUNT(*) FROM playable_library_tracks').fetchone()[0]
        covers = [dict(row) for row in self.conn.execute(
            'SELECT video_id,thumbnail FROM playable_library_tracks ORDER BY identity_key LIMIT 6')]
        modes = self.conn.execute('SELECT COUNT(*) FROM taste_modes WHERE active=1').fetchone()[0]
        return {'songs': songs, 'covers': covers, 'modes': modes}

    @snapshot_read
    def library_sample(self, per_genre, *, maximum_duration):
        """Bound each genre bucket to the number the caller can display."""
        eligible = '(duration_s IS NULL OR duration_s<=?)'
        genres = [row[0] for row in self.conn.execute(
            'SELECT DISTINCT genre FROM playable_library_tracks WHERE ' + eligible, (maximum_duration,))]
        return [dict(row) for genre in genres for row in self.conn.execute(
            'SELECT identity_key k,title,artist,album,video_id vid,thumbnail thumb,genre,duration_s duration '
            'FROM playable_library_tracks WHERE genre IS ? AND ' + eligible + ' ORDER BY random() LIMIT ?',
            (genre, maximum_duration, per_genre))]

    @lookup_read
    def playable_owned(self, *, exclude_generated, maximum_duration):
        quarantine = (
            ' AND (NOT EXISTS(SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id '
            'JOIN quarantined_playlists q ON q.playlist_scope_id=p.playlist_scope_id WHERE pt.track_id=t.id) '
            'OR EXISTS(SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id '
            'LEFT JOIN quarantined_playlists q ON q.playlist_scope_id=p.playlist_scope_id '
            'WHERE pt.track_id=t.id AND q.playlist_scope_id IS NULL))' if exclude_generated else '')
        return self.conn.execute('SELECT 1 FROM playable_library_tracks t WHERE '
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
    def has_vibe_evidence(self, prefix, *, sources, minimum_support):
        version = vibe_versions.expression(self.conn, 'recording_vibes', 'v')
        return self.conn.execute(_SCOPE +
            'SELECT 1 FROM recording_vibes v JOIN scope s ON s.recording_id=v.recording_id '
            f"WHERE v.support>? AND v.source IN ({','.join('?' for _ in sources)}) "
            'AND substr(' + version + ',1,?)=? AND length(' + version + ')>? LIMIT 1',
            (minimum_support, *sources, len(prefix), prefix, len(prefix))).fetchone() is not None

    @snapshot_read
    def vibe_evidence(self, prefix, *, measured_sources, proxy_sources, minimum_support):
        version = vibe_versions.expression(self.conn, 'recording_vibes', 'v')
        return tuple(self.conn.execute(_SCOPE +
            f"SELECT COUNT(DISTINCT CASE WHEN v.source IN ({','.join('?' for _ in measured_sources)}) "
            f"THEN v.recording_id END), COUNT(DISTINCT CASE WHEN v.source IN ({','.join('?' for _ in proxy_sources)}) "
            "THEN v.recording_id END) FROM recording_vibes v JOIN scope s ON s.recording_id=v.recording_id "
            'WHERE v.support>? AND substr(' + version + ',1,?)=? AND length(' + version + ')>?',
            (*measured_sources, *proxy_sources, minimum_support, len(prefix), prefix, len(prefix))).fetchone())

    @snapshot_read
    def journey_history(self, *, provider, completed_statuses):
        takeout = self.conn.execute(
            'SELECT COUNT(DISTINCT e.id) FROM play_events e JOIN play_event_sources s ON s.event_id=e.id '
            "WHERE s.source='takeout' AND e.played_at IS NOT NULL AND e.video_id IS NOT NULL").fetchone()[0]
        config = self.conn.execute(
            'SELECT status,fingerprint FROM enrichment_provider_config WHERE provider=?', (provider,)).fetchone()
        checked = self.conn.execute(_SCOPE +
            'SELECT COUNT(*) FROM scope s WHERE EXISTS (SELECT 1 FROM enrichment_provider_state p '
            f"WHERE p.recording_id=s.recording_id AND p.provider=? AND p.status IN ({','.join('?' for _ in completed_statuses)})) "
            'AND NOT EXISTS (SELECT 1 FROM enrichment_provider_jobs j '
            'WHERE j.recording_id=s.recording_id AND j.provider=?)',
            (provider, *completed_statuses, provider)).fetchone()[0]
        return {'live_starts': self.conn.execute("SELECT 1 FROM play_event_sources WHERE source='live' LIMIT 1").fetchone() is not None,
            'takeout': takeout,
            'takeout_started': self.conn.execute('SELECT 1 FROM takeout_imports LIMIT 1').fetchone() is not None,
            'lastfm_config': tuple(config) if config else None, 'lastfm_checked': checked,
            'created_playlists': self.conn.execute(
                "SELECT COUNT(DISTINCT playlist_scope_id) FROM playlists WHERE created_at_source='local'").fetchone()[0]}
