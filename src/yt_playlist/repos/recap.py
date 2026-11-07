"""SQLite facts used to prepare the monthly recap."""
from yt_playlist.repos.base import Repo, synchronized


class RecapRepo(Repo):
    @synchronized
    def plays(self, since, until, *, taste_filter, quarantine_group):
        return [dict(r) for r in self.conn.execute(
            "SELECT pe.id,account_id, identity_key, video_id, played_at, playlist_ytm_id,c.day "
            "FROM play_event_subjects pe JOIN play_event_calendar c ON c.event_id=pe.id "
            f"WHERE c.day>=date(?,'unixepoch') AND c.day<date(?,'unixepoch') AND {taste_filter} "
            "ORDER BY played_at, pe.id",
            (since, until, quarantine_group))]

    @synchronized
    def play_sources(self, event_ids):
        event_ids = list(dict.fromkeys(event_ids))
        sources = {}
        for offset in range(0, len(event_ids), 400):
            page = event_ids[offset:offset + 400]
            qs = ','.join('?' for _ in page)
            for event_id, source in self.conn.execute(
                    f'SELECT event_id,source FROM play_event_sources WHERE event_id IN ({qs})', page):
                sources.setdefault(event_id, []).append(source)
        return sources

    @synchronized
    def first_plays(self, until, *, taste_filter, quarantine_group):
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT identity_key, MIN(CAST(strftime('%s',c.day) AS REAL)) FROM play_event_subjects pe "
            "JOIN play_event_calendar c ON c.event_id=pe.id "
            f"WHERE c.day<date(?,'unixepoch') AND {taste_filter} "
            "GROUP BY identity_key", (until, quarantine_group))}

    @synchronized
    def first_observations(self, until):
        return {r[0]: r[1] for r in self.conn.execute(
                "SELECT identity_key,MIN(day_ts) FROM listening_days WHERE day_ts<? "
                "GROUP BY identity_key", (until,))}

    @synchronized
    def source_spans(self):
        return {(r[0], r[1]): (r[2], r[3]) for r in self.conn.execute(
            "SELECT pe.account_id,s.source,MIN(CAST(strftime('%s',c.day) AS REAL)),"
            "MAX(CAST(strftime('%s',c.day) AS REAL)) FROM play_events pe "
            "JOIN play_event_calendar c ON c.event_id=pe.id "
            "JOIN play_event_sources s ON s.event_id=pe.id GROUP BY pe.account_id,s.source")}

    @synchronized
    def track_metadata(self):
        return [dict(row) for row in self.conn.execute(
            'SELECT identity_key,title,artist,genre,mb_year FROM tracks ORDER BY id')]

    @synchronized
    def track_albums(self):
        return [tuple(row) for row in self.conn.execute(
                f"SELECT DISTINCT t.identity_key,p.provider_id FROM tracks t JOIN {self.album_tracks_table} at ON at.entry_id=t.id "
                "JOIN album_primary_provider_ids p ON p.album_id=at.album_id AND p.provider='youtube'")]

    @synchronized
    def artist_labels(self):
        return dict(self.conn.execute('SELECT id,name FROM artists'))

    @synchronized
    def track_artists(self):
        return [tuple(row) for row in self.conn.execute(
            'SELECT r.identity_key,a.artist_id FROM recording_listening_artists a '
            'JOIN recordings r ON r.id=a.recording_id ORDER BY a.artist_id')]

    @synchronized
    def familiar_artists(self, *, quarantine_group):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT a.artist_id FROM playlist_tracks pt JOIN catalog_entries t ON t.id=pt.track_id "
            "JOIN recording_listening_artists a ON a.recording_id=t.recording_id "
            "JOIN playlists p ON p.id=pt.playlist_id WHERE p.playlist_scope_id NOT IN "
            "(SELECT playlist_scope_id FROM quarantined_playlists WHERE name=?) UNION SELECT a.artist_id FROM account_albums m "
            "JOIN album_artists a ON a.album_id=m.album_id", (quarantine_group,))}

    @synchronized
    def full_album_tracks(self):
        return [dict(row) for row in self.conn.execute(
                "SELECT a.id,a.title,a.artist,a.thumbnail,p.provider_id browse_id, "
                f"COALESCE(r.identity_key,at.legacy_track_key) track_key FROM {self.album_fetch_state_table} f "
                "JOIN albums a ON a.id=f.album_id JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
                f"JOIN {self.album_tracks_table} at ON at.album_id=a.id AND at.is_full=1 "
                "LEFT JOIN catalog_entries e ON e.id=at.entry_id "
                "LEFT JOIN recordings r ON r.id=COALESCE(e.recording_id,at.recording_id) WHERE f.complete=1")]

    @synchronized
    def created_playlists(self, since, until, *, quarantine_group, creation_sources):
        from yt_playlist.core import playlist_recipe_state
        recipe_scope = 'playlist_scope_id' if playlist_recipe_state.available(self.conn) else 'playlist_ytm'
        qs = ','.join('?' for _ in creation_sources)
        return [dict(r) for r in self.conn.execute(
            "WITH candidates AS (SELECT provider_playlist_id,created_at,account_id,"
            "MIN(title) OVER (PARTITION BY provider_playlist_id) title,"
            "MAX(track_count) OVER (PARTITION BY provider_playlist_id) track_count,"
            "ROW_NUMBER() OVER (PARTITION BY provider_playlist_id ORDER BY created_at,id) first_created "
            "FROM playlists "
            f"WHERE created_at>=? AND created_at<? AND created_at_source IN ({qs}) "
            "AND playlist_scope_id NOT IN (SELECT playlist_scope_id FROM quarantined_playlists WHERE name=?) "
            f"AND playlist_scope_id NOT IN (SELECT {recipe_scope} FROM rec_recipes) "
            ") SELECT provider_playlist_id,title,created_at,track_count,account_id "
            "FROM candidates WHERE first_created=1 ORDER BY created_at,provider_playlist_id",
            (since, until, *creation_sources, quarantine_group))]

    @synchronized
    def calendars(self):
        return dict(self.conn.execute('SELECT account_id,timezone FROM listening_calendars'))
