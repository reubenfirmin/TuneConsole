"""SQLite facts for recommendation training and causal evaluation."""
import datetime as dt
import json

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.catalog_query import LIBRARY_TRACKS_SQL
from yt_playlist.core.listening_calendar import zone
from yt_playlist.repos.base import Repo, synchronized, snapshot_read

_EPOCH = dt.date(1970, 1, 1)


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

    @snapshot_read
    def evaluation_listening(self, end, *, start=None):
        """Organic activity inside a window, with a date bucket for each observation."""
        params = {'start': start, 'end': end}
        rows = [(account, key, day, int(at // 86400)) for account, key, day, at in self.conn.execute(
            'SELECT account_id,identity_key,day,at FROM listening_activity '
            "WHERE precision='exact_timestamp' AND at<:end AND (:start IS NULL OR at>=:start)", params)]
        coarse = self.conn.execute(
            'WITH coarse AS MATERIALIZED ('
            'SELECT account_id,identity_key,recording_id,day FROM listening_activity WHERE event_id IS NULL '
            "AND (:start IS NULL OR day>=date(:start,'unixepoch','-1 day')) "
            "AND day<=date(:end,'unixepoch','+1 day')) "
            'SELECT c.account_id,c.identity_key,c.day,o.source_day,o.timezone FROM coarse c '
            'JOIN listening_observations o ON o.account_id=c.account_id AND o.day=c.day '
            'AND (o.recording_id=c.recording_id OR (o.recording_id IS NULL AND o.unresolved_subject_key=c.identity_key)) '
            "WHERE o.precision='calendar_day' AND o.first_observed_at<:end", params)
        # Ignore a day that straddles a boundary. Local midnights handle 23- and
        # 25-hour days, and the capture time keeps later syncs out of the window.
        buckets = {}
        for account, key, day, source_day, timezone in coarse:
            check_cancellable_work()
            scope = (source_day, timezone)
            if scope not in buckets:
                date, tz = dt.date.fromisoformat(source_day), zone(timezone)
                bucket = None
                if tz is not None:
                    first = dt.datetime.combine(date, dt.time.min, tz).timestamp()
                    last = dt.datetime.combine(date + dt.timedelta(days=1), dt.time.min, tz).timestamp()
                    if (start is None or start <= first) and first < last <= end:
                        bucket = (date - _EPOCH).days
                buckets[scope] = bucket
            if buckets[scope] is not None:
                rows.append((account, key, day, buckets[scope]))
        return rows

    @synchronized
    def eligible_keys(self, *, maximum_duration, excluded_video_types):
        return {row[0] for row in self.conn.execute(
            f'SELECT DISTINCT identity_key FROM ({LIBRARY_TRACKS_SQL}) '
            'WHERE (video_type IS NULL OR video_type NOT IN (SELECT value FROM json_each(?))) '
            'AND (duration_s IS NULL OR duration_s<=?)', (json.dumps(excluded_video_types), maximum_duration))}

    @synchronized
    def playlist_memberships(self, *, excluded=(), included=None):
        return [tuple(row) for row in self.conn.execute(
            'SELECT pt.playlist_id,t.identity_key FROM playlist_tracks pt JOIN catalog_tracks t ON t.id=pt.track_id '
            'WHERE pt.playlist_id NOT IN (SELECT value FROM json_each(:excluded)) '
            'AND (:included IS NULL OR pt.playlist_id IN (SELECT value FROM json_each(:included)))',
            {'excluded': json.dumps(list(excluded)),
             'included': json.dumps(list(included)) if included is not None else None})]

    @synchronized
    def album_memberships(self):
        return [tuple(row) for row in self.conn.execute(
            f'SELECT at.album_id,t.identity_key FROM ({LIBRARY_TRACKS_SQL}) t JOIN {self.album_tracks_table} at ON at.entry_id=t.id')]

    @synchronized
    def artist_memberships(self):
        return [tuple(row) for row in self.conn.execute(
            f'SELECT ta.artist_id,t.identity_key FROM ({LIBRARY_TRACKS_SQL}) t JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id')]

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
            f"SELECT DISTINCT genre,mb_year year,identity_key key FROM ({LIBRARY_TRACKS_SQL}) "
            "WHERE genre<>'' OR mb_year<>''")]

    @synchronized
    def unchanged_playlists(self, cutoff):
        return {row[0] for row in self.conn.execute(
            'SELECT id FROM active_playlists WHERE first_seen IS NOT NULL AND first_seen<=? '
            'AND last_changed IS NOT NULL AND last_changed<=?', (cutoff, cutoff))}

    @synchronized
    def dated_keys(self, cutoff):
        return {row[0] for row in self.conn.execute(
            f'SELECT DISTINCT identity_key FROM ({LIBRARY_TRACKS_SQL}) WHERE created_at IS NOT NULL AND created_at<=?', (cutoff,))}

    @synchronized
    def known_playlist_count(self, cutoff):
        return self.conn.execute('SELECT COUNT(*) FROM active_playlists WHERE first_seen IS NOT NULL AND first_seen<=?',
                                 (cutoff,)).fetchone()[0]
