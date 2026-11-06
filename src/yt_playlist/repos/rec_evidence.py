"""SQLite facts for recommendation training and causal evaluation."""
import json

from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class RecEvidenceRepo(Repo):
    @synchronized
    def evaluation_history_bounds(self):
        # Generated plays still establish the clock for the holdout interval.
        return tuple(self.conn.execute(
            'SELECT MIN(at),MAX(at) FROM (SELECT at FROM listening_activity '
            'UNION ALL SELECT played_at FROM play_events WHERE is_generated=1)').fetchone())

    @snapshot_read
    def generated_only_keys_between(self, start_ts, end_ts, group='Generated'):
        rows = self.conn.execute(
            'WITH independent_keys AS MATERIALIZED (SELECT DISTINCT CAST(identity_key AS TEXT) identity_key '
            'FROM listening_days WHERE day_ts>=? AND day_ts<?) '
            'SELECT pe.identity_key k,SUM(CASE WHEN pe.is_generated=1 THEN 0 ELSE 1 END) non_generated '
            'FROM play_event_subjects pe WHERE pe.played_at>=? AND pe.played_at<? GROUP BY pe.identity_key '
            'HAVING NOT EXISTS (SELECT 1 FROM independent_keys d WHERE d.identity_key=pe.identity_key)',
            (start_ts, end_ts, start_ts, end_ts)).fetchall()
        return {row['k'] for row in rows if row['non_generated'] == 0}

    @synchronized
    def eligible_keys(self, *, maximum_duration, excluded_video_types):
        return {row[0] for row in self.conn.execute(
            'SELECT DISTINCT identity_key FROM tracks '
            'WHERE (video_type IS NULL OR video_type NOT IN (SELECT value FROM json_each(?))) '
            'AND (duration_s IS NULL OR duration_s<=?)', (json.dumps(excluded_video_types), maximum_duration))}

    @synchronized
    def playlist_memberships(self, *, excluded=(), included=None):
        return [tuple(row) for row in self.conn.execute(
            'SELECT pt.playlist_id,t.identity_key FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id '
            'WHERE pt.playlist_id NOT IN (SELECT value FROM json_each(:excluded)) '
            'AND (:included IS NULL OR pt.playlist_id IN (SELECT value FROM json_each(:included)))',
            {'excluded': json.dumps(list(excluded)),
             'included': json.dumps(list(included)) if included is not None else None})]

    @synchronized
    def album_memberships(self):
        return [tuple(row) for row in self.conn.execute(
            f'SELECT at.album_id,t.identity_key FROM tracks t JOIN {self.album_tracks_table} at ON at.entry_id=t.id')]

    @synchronized
    def artist_memberships(self):
        return [tuple(row) for row in self.conn.execute(
            f'SELECT ta.artist_id,t.identity_key FROM tracks t JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id')]

    @synchronized
    def session_memberships(self):
        return [tuple(row) for row in self.conn.execute('SELECT snapshot_id,identity_key FROM listening_daily_items')]

    @synchronized
    def dated_session_memberships(self, cutoff):
        return [tuple(row) for row in self.conn.execute(
            "SELECT DISTINCT account_id||':'||day,identity_key FROM listening_activity WHERE at<?", (cutoff,))]

    @synchronized
    def content_facets(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT genre,mb_year year,identity_key key FROM tracks WHERE genre<>'' OR mb_year<>''")]

    @synchronized
    def unchanged_playlists(self, cutoff):
        return {row[0] for row in self.conn.execute(
            'SELECT id FROM playlists WHERE first_seen IS NOT NULL AND first_seen<=? '
            'AND last_changed IS NOT NULL AND last_changed<=?', (cutoff, cutoff))}

    @synchronized
    def dated_keys(self, cutoff):
        return {row[0] for row in self.conn.execute(
            'SELECT DISTINCT identity_key FROM tracks WHERE created_at IS NOT NULL AND created_at<=?', (cutoff,))}

    @synchronized
    def known_playlist_count(self, cutoff):
        return self.conn.execute('SELECT COUNT(*) FROM playlists WHERE first_seen IS NOT NULL AND first_seen<=?',
                                 (cutoff,)).fetchone()[0]
