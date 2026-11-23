"""Listening days, exact plays, and retained source evidence."""
from contextlib import contextmanager
from yt_playlist.core import catalog_keys, listening, rating_actions, worker_settings
from yt_playlist.core import listening_views
from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class HistoryRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic(join=True):
            yield

    @synchronized
    def day_count(self, account):
        return listening.day_count(self.conn, account)

    @snapshot_read
    def calendar_events(self, account):
        return [dict(row) for row in self.conn.execute('SELECT id,played_at FROM play_events WHERE account_id=?', (account,))]

    @snapshot_read
    def calendar_accounts(self, account=None):
        return [row[0] for row in self.conn.execute('SELECT id FROM accounts WHERE listening_timezone IS NOT NULL' +
            ('' if account is None else ' AND id=?'), () if account is None else (account,))]

    @synchronized
    def calendar_facts(self, account_id):
        row = self.conn.execute('SELECT listening_timezone AS timezone,listening_timezone_confirmed AS confirmed '
            'FROM accounts WHERE id=? AND listening_timezone IS NOT NULL', (account_id,)).fetchone()
        return dict(row) if row else None

    @snapshot_read
    def listening_calendars(self):
        return dict(self.conn.execute('SELECT id,listening_timezone FROM accounts WHERE listening_timezone IS NOT NULL'))

    @synchronized
    def set_calendar(self, account_id, name, *, confirmed, event_days=None):
        with self.conn.atomic(join=True):
            result = self.conn.execute('UPDATE accounts SET listening_timezone=?,listening_timezone_confirmed=? '
                'WHERE id=?', (name, int(confirmed), account_id))
            if not result.rowcount:
                raise ValueError('Listening calendar requires a recorded account')
            if event_days is not None:
                self.conn.executemany('UPDATE play_events SET day=? WHERE id=? AND day IS NOT ?',
                    ((day, event, day) for event, day in event_days))
                self.conn.execute('UPDATE listening_observations SET day=CASE WHEN timezone=? THEN source_day END '
                    "WHERE account_id=? AND precision='calendar_day'", (name, account_id))

    @synchronized
    def record_days(self, account_id, taken_at, observations, timezone):
        """Persist prepared subjects and calendar-day projections."""
        with self.conn.atomic():
            for key, subject, recording, source_day, day, source in observations:
                self.conn.execute('INSERT INTO main.listening_observations(account_id,raw_identity_key,unresolved_subject_key,recording_id,'
                    'source_day,day,source,timezone,precision,first_observed_at,last_observed_at) '
                    "VALUES (?,?,?,?,?,?,?,?,'calendar_day',?,?) "
                    'ON CONFLICT(account_id,raw_identity_key,source_day,source,timezone) DO UPDATE SET '
                    'first_observed_at=min(first_observed_at,excluded.first_observed_at),'
                    'last_observed_at=max(last_observed_at,excluded.last_observed_at)',
                    (account_id, key, None if recording else subject, recording,
                     source_day, day, source, timezone, taken_at, taken_at))

    @synchronized
    def reset_play_history(self, account_id=None) -> None:
        """Clear coarse captures for a fresh sync, retaining the exact-event ledger.

        Scoped to one account, or all when None. Exact plays continue to contribute
        listening days; a history-window reset does not delete timestamped evidence.
        """
        with self.conn:
            if account_id is None:
                self.conn.execute('DELETE FROM listening_observations')
            else:
                self.conn.execute('DELETE FROM listening_observations WHERE account_id=?',(account_id,))

    @synchronized
    def history_keys_before(self, ts) -> set[str]:
        """Keys strictly before a cutoff, using exact times where known.

        Coarse dates retain their date-key timestamp; an exact event must never
        enter earlier context merely because its account day starts before it.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT identity_key FROM listening_activity WHERE at<?",
            (ts,)).fetchall()
        return {r["identity_key"] for r in rows}

    @synchronized
    def history_keys_between(self, start_ts, end_ts) -> set[str]:
        """Distinct activity keys in the half-open interval [start_ts, end_ts)."""
        rows = self.conn.execute(
            "SELECT DISTINCT identity_key FROM listening_activity "
            "WHERE at>=? AND at<?", (start_ts, end_ts)).fetchall()
        return {r["identity_key"] for r in rows}

    @synchronized
    def play_counts_before(self, ts) -> dict[str, int]:
        """Per-key day-model play counts strictly before an evaluation cutoff."""
        rows = self.conn.execute(
            "SELECT identity_key k,COUNT(*) c FROM ("
            "SELECT DISTINCT account_id,identity_key,day FROM listening_activity WHERE at<?) "
            "GROUP BY identity_key", (ts,)).fetchall()
        return {r["k"]: int(r["c"]) for r in rows}

    @synchronized
    def recent_plays_with_ts(self, limit=None, exclude_list_ids=None) -> list:
        """Newest activity per key, retaining exact times when available.

        Excluded playlist events suppress only their account/day sync shadows.
        Independent and unknown coarse evidence survives; adjacent dates are not guessed.
        """
        prefix, names = listening_views.materialized()
        events = names.get('play_event_subjects', 'play_event_subjects')
        eligible = names.get('eligible_listening_evidence', 'eligible_listening_evidence')
        if exclude_list_ids:
            excl = list(exclude_list_ids)
            ph = ",".join("?" * len(excl))
            q = ("WITH " + (prefix + ',' if prefix else '') +
                 "kept AS MATERIALIZED (SELECT pe.account_id,pe.identity_key,pe.played_at,c.day "
                 f"FROM {events} pe JOIN play_event_calendar c ON c.event_id=pe.id "
                 f"WHERE COALESCE(pe.is_generated,0)=0 AND (pe.playlist_ytm_id IS NULL OR pe.playlist_ytm_id NOT IN ({ph}))"
                 ") "
                 "SELECT k, MAX(ts) ts FROM ("
                 "  SELECT identity_key k, played_at ts FROM kept"
                 "  UNION ALL"
                 "  SELECT e.identity_key k,CAST(strftime('%s',e.day||' 12:00:00') AS REAL) ts"
                 f"  FROM {eligible} e WHERE e.event_id IS NULL"
                 "  AND NOT EXISTS (SELECT 1 FROM kept k WHERE k.account_id=e.account_id "
                 "    AND k.identity_key=e.identity_key AND k.day=e.day)"
                 "  AND (e.source<>'ytm_history' OR NOT EXISTS ("
                 f"    SELECT 1 FROM {events} pe JOIN play_event_calendar c ON c.event_id=pe.id"
                 "    WHERE pe.account_id=e.account_id AND pe.identity_key=e.identity_key"
                 f"    AND pe.playlist_ytm_id IN ({ph})"
                 "    AND c.day=e.day))"
                 ") GROUP BY k ORDER BY ts DESC")
            params: tuple = (*excl, *excl)
        else:
            activity = names.get('listening_activity', 'listening_activity')
            q = (('WITH ' + prefix + ' ' if prefix else '') +
                 f"SELECT identity_key k,MAX(at) ts FROM {activity} GROUP BY identity_key ORDER BY ts DESC")
            params = ()
        rows = self.conn.execute(q + (" LIMIT ?" if limit else ""),
                                 (*params, limit) if limit else params).fetchall()
        return [(r["k"], float(r["ts"])) for r in rows]

    @snapshot_read
    def recent_calendar_evidence(self, first_day, last_day, now, excluded):
        # Resolve exact event shadows once for the requested calendar window.
        # Ordinary and excluded-playlist events suppress every coarse source.
        # Other generated events suppress only YouTube Music history shadows.
        extra = (' OR pe.playlist_ytm_id IN (' + ','.join('?' for _ in excluded) + ')') if excluded else ''
        return [dict(row) for row in self.conn.execute(
            'WITH exact AS MATERIALIZED ('
            'SELECT pe.account_id,pe.identity_key,c.day,'
            'MAX(CASE WHEN COALESCE(pe.is_generated,0)=0' + extra + ' THEN 1 ELSE 0 END) suppress_all '
            'FROM play_event_calendar c JOIN play_event_subjects pe ON pe.id=c.event_id '
            'WHERE c.day BETWEEN ? AND ? AND pe.played_at<=? '
            'GROUP BY pe.account_id,pe.identity_key,c.day) '
            'SELECT o.account_id,o.identity_key,o.day,o.timezone,MIN(o.source) AS source '
            'FROM listening_observation_subjects o LEFT JOIN exact e '
            'ON e.account_id=o.account_id AND e.identity_key=o.identity_key AND e.day=o.day '
            'WHERE o.day BETWEEN ? AND ? AND o.first_observed_at<=? '
            "AND o.precision='calendar_day' AND (e.day IS NULL OR (e.suppress_all=0 AND o.source IS NOT 'ytm_history')) "
            'GROUP BY o.account_id,o.identity_key,o.day,o.timezone',
            (*excluded, first_day, last_day, now, first_day, last_day, now))]

    @synchronized
    def last_plays_for(self, keys) -> dict:
        """Immutable serving metadata for just the offered identities."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        return {r["identity_key"]: float(r["last"]) for r in self.conn.execute(
            f"SELECT identity_key, MAX(at) last FROM listening_activity "
            f"WHERE identity_key IN ({placeholders}) GROUP BY identity_key", keys)}

    @synchronized
    def playlist_quarantined(self, account_id, playlist_ytm_id, group):
        return bool(self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM quarantined_playlists q "
            "JOIN playlist_scopes s ON s.id=q.playlist_scope_id WHERE q.name=? "
            "AND (s.provider_playlist_id=? OR 'RDAMPL' || s.provider_playlist_id=?) "
            "AND (s.account_id IS NULL OR s.account_id=?))",
            (group, playlist_ytm_id, playlist_ytm_id, account_id)).fetchone()[0])

    @snapshot_read
    def subject_facts(self, key, video):
        exact = self.conn.execute('SELECT r.id,r.identity_key FROM track_sources y '
            'JOIN recordings r ON r.id=y.recording_id WHERE y.video_id=?', (video,)).fetchone()
        direct = self.conn.execute('SELECT id,identity_key FROM recordings WHERE identity_key=?', (key,)).fetchone()
        if direct:
            direct = self.conn.execute('SELECT id,identity_key FROM recordings WHERE id=?',
                (catalog_keys.resolve_recording(self.conn, direct['id']),)).fetchone()
        aliases = self.conn.execute('SELECT DISTINCT r.id,r.identity_key FROM recording_aliases a '
            'JOIN recordings r ON r.id=a.recording_id WHERE a.alias=?', (key,)).fetchall()
        return {'exact': dict(exact) if exact else None, 'direct': dict(direct) if direct else None,
                'aliases': [dict(row) for row in aliases]}

    @snapshot_read
    def latest_event(self, account):
        row = self.conn.execute('SELECT p.*,EXISTS(SELECT 1 FROM play_event_sources s '
            "WHERE s.event_id=p.id AND s.source='live') live FROM play_event_subjects p "
            'WHERE account_id=? ORDER BY played_at DESC,id DESC LIMIT 1', (account,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def append_event(self, account, key, timestamp, *, subject, recording, video_id=None,
                     playlist_ytm_id=None, like_status=None, is_generated=None):
        return self.conn.execute('INSERT INTO play_events(account_id,raw_identity_key,'
            'recording_id,unresolved_subject_key,video_id,played_at,playlist_ytm_id,like_status,is_generated,membership_ref_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)', (account,key,recording,None if recording else subject,video_id,timestamp,
                playlist_ytm_id,like_status,is_generated,listening.membership_reference(self.conn,account,playlist_ytm_id))).lastrowid

    @synchronized
    def update_event(self, event, account, video, playlist, rating, generated, *, context_choice=False):
        membership_args = (playlist, listening.membership_reference(self.conn, account, playlist))
        self.conn.execute('UPDATE main.play_events SET like_status=COALESCE(?,like_status),'
            "video_id=COALESCE(NULLIF(trim(video_id),''),?),"
            'membership_ref_id=CASE WHEN playlist_ytm_id IS NULL AND ? IS NOT NULL '
            'THEN ? ELSE membership_ref_id END,'
            'is_generated=CASE WHEN playlist_ytm_id IS NULL AND ? IS NOT NULL THEN ? '
            'WHEN ? AND playlist_ytm_id IS ? THEN 1 ELSE is_generated END,'
            'playlist_ytm_id=COALESCE(playlist_ytm_id,?) WHERE id=?',
            (rating, video, *membership_args, playlist, generated, context_choice, playlist, playlist, event))

    @synchronized
    def project_event(self, event, day, source):
        listening.store_event_day(self.conn, event, day, source)

    @snapshot_read
    def matching_event(self, account_id, subject, video_id, timestamp, tolerance, *, verified_playlist=None,
                       selected_at=None):
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?',(subject,)).fetchone()
        scope,args = ('recording_id=?',(recording[0],)) if recording else (
            'recording_id IS NULL AND unresolved_subject_key=?',(subject,))
        return self.conn.execute(
            'SELECT p.*,EXISTS(SELECT 1 FROM play_event_sources s WHERE s.event_id=p.id '
            f"AND s.source='live') live FROM play_event_subjects p WHERE account_id=? AND {scope} "
            "AND (NULLIF(trim(video_id),'') IS NULL OR NULLIF(trim(?),'') IS NULL OR video_id=?) "
            'AND (? IS NULL OR playlist_ytm_id IS NULL OR playlist_ytm_id=?) '
            'AND (? IS NULL OR played_at>=?) '
            'AND played_at BETWEEN ?-? AND ?+? ORDER BY ABS(played_at-?),id LIMIT 1',
            (account_id,*args,video_id,video_id,verified_playlist,verified_playlist,
             selected_at,selected_at,timestamp,tolerance,timestamp,tolerance,timestamp)).fetchone()

    @synchronized
    def listening_comparison_coverage(self, since, until, *, exclude_list_ids=()) -> dict:
        """Descriptive coverage, including plays excluded from taste learning.

        A dated observation is reported as a date, never converted into a play timestamp.
        Keep the source count independent of content-vector and music-group availability.
        """
        excluded = list(dict.fromkeys(exclude_list_ids))
        eligible = "1"
        if excluded:
            slots = ",".join("?" for _ in excluded)
            eligible = f"(playlist_ytm_id IS NULL OR playlist_ytm_id NOT IN ({slots}))"
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT identity_key) tracks,"
            f"COUNT(DISTINCT CASE WHEN {eligible} THEN identity_key END) eligible_tracks "
            "FROM play_event_subjects WHERE played_at>=? AND played_at<=?",
            [*excluded, since, until]).fetchone()
        # Read the descriptive sources directly. The learning projection excludes some generated
        # listening and performs reconciliation work that this coverage explanation doesn't need.
        latest_day = self.conn.execute(
            "SELECT MAX(day) FROM (SELECT MAX(day) day FROM listening_observations "
            "UNION ALL SELECT MAX(day) day FROM play_event_calendar)").fetchone()[0]
        return {"tracks": row["tracks"], "eligible_tracks": row["eligible_tracks"],
                "latest_day": latest_day}

    @snapshot_read
    def latest_dated_listening(self):
        return self.conn.execute('SELECT MAX(day) FROM listening_observations').fetchone()[0]

    @snapshot_read
    def browse_playback_onset(self, account, video, since, until):
        row = self.conn.execute(
            'SELECT e.played_at,e.playlist_ytm_id,e.is_generated FROM play_events e '
            "WHERE e.account_id=? AND e.video_id=? AND e.played_at BETWEEN ? AND ? "
            "AND EXISTS (SELECT 1 FROM play_event_sources s WHERE s.event_id=e.id AND s.source='live') "
            'ORDER BY e.played_at DESC,e.id DESC LIMIT 1', (account, video, since, until)).fetchone()
        return row['played_at'] if row and row['playlist_ytm_id'] is None and row['is_generated'] else None

    @synchronized
    def play_events_since(self, since_ts, exclude_list_ids=None, *, taste_only=False, include_live=False) -> list[dict]:
        """#75 Play events at/after since_ts, oldest first.

        `exclude_list_ids`, when given, drops rows whose playlist_ytm_id is in that set (#93v2: a
        caller reading recent plays as mood/taste EVIDENCE passes the radio playlists here, so the
        radio's own machine-queued plays cannot feed back into the model that steers the radio - see
        rec/transient.py's radio_list_ids for the loop this closes). Playlist exclusions keep rows
        with a NULL playlist ID. Taste reads additionally apply verified attribution, including
        album playback matched by browse ID alone."""
        # Taste consumers additionally retain generated provenance after a playlist is
        # removed. Descriptive callers keep the complete event stream by default.
        evidence = ("AND COALESCE(is_generated,0)=0 AND NOT EXISTS "
                    "(SELECT 1 FROM quarantined_playlists q JOIN playlist_scopes p ON p.id=q.playlist_scope_id "
                    "WHERE p.provider_playlist_id=e.playlist_ytm_id AND (p.account_id IS NULL OR p.account_id=e.account_id)) "
                    if taste_only else "")
        fields = 'account_id, identity_key, video_id, played_at, playlist_ytm_id, like_status'
        if taste_only or include_live:
            fields += (', EXISTS(SELECT 1 FROM play_event_sources s WHERE s.event_id=e.id '
                       "AND s.source='live') AS live")
        if exclude_list_ids:
            excl = list(exclude_list_ids)
            placeholders = ",".join("?" * len(excl))
            q = (f"SELECT {fields} FROM play_event_subjects e WHERE played_at>=? "
                 f"AND (playlist_ytm_id IS NULL OR playlist_ytm_id NOT IN ({placeholders})) "
                 + evidence + "ORDER BY played_at, id")
            rows = self.conn.execute(q, (since_ts, *excl)).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT {fields} FROM play_event_subjects e WHERE played_at>=? " + evidence + "ORDER BY played_at, id", (since_ts,)).fetchall()
        return [dict(r) for r in rows]

    @snapshot_read
    def live_play_activity(self, since, until):
        return [dict(row) for row in self.conn.execute(
            'SELECT e.account_id,e.played_at AS at FROM play_events e '
            'CROSS JOIN play_event_sources s ON s.event_id=e.id '
            "WHERE e.played_at BETWEEN ? AND ? AND s.source='live'", (since, until))]

    @snapshot_read
    def taste_replay_page(self, after, until, *, limit):
        """Original exact observations, ordered by occurrence and a stable tie-breaker."""
        return [dict(row) for row in self.conn.execute(
            'SELECT e.id,e.account_id,e.identity_key,e.played_at,c.day,e.playlist_ytm_id '
            'FROM play_event_subjects e JOIN play_event_calendar c ON c.event_id=e.id '
            'WHERE (e.played_at,e.id)>(?,?) AND e.played_at<=? AND COALESCE(e.is_generated,0)=0 '
            'AND NOT EXISTS (SELECT 1 FROM quarantined_playlists q JOIN playlist_scopes p ON p.id=q.playlist_scope_id '
            'WHERE p.provider_playlist_id=e.playlist_ytm_id AND (p.account_id IS NULL OR p.account_id=e.account_id)) '
            'AND EXISTS (SELECT 1 FROM play_event_sources s WHERE s.event_id=e.id) '
            'ORDER BY e.played_at,e.id LIMIT ?', (*after, until, limit))]

    @snapshot_read
    def taste_rating_page(self, after, until, *, limit):
        return [dict(row) for row in self.conn.execute(
            'SELECT a.id,a.account_id,r.identity_key,a.at played_at,cal.listening_timezone AS timezone '
            f'FROM {rating_actions.VIEW} a JOIN track_sources y USING(video_id) JOIN recordings r ON r.id=y.recording_id '
            'LEFT JOIN accounts cal ON cal.id=a.account_id '
            "WHERE a.status='LIKE' AND a.source IN ('api','action') AND (a.at,a.id)>(?,?) "
            'AND a.at<=? ORDER BY a.at,a.id LIMIT ?', (*after, until, limit))]

    @snapshot_read
    def taste_rating_revision(self):
        return self.conn.execute(f'SELECT MAX(id) FROM {rating_actions.VIEW}').fetchone()[0]

    @snapshot_read
    def next_taste_evidence(self, after):
        return self.conn.execute(
            'SELECT MIN(at) FROM (SELECT MIN(played_at) AS at FROM play_event_subjects WHERE played_at>? '
            f"UNION ALL SELECT MIN(at) FROM {rating_actions.VIEW} WHERE status='LIKE' "
            "AND source IN ('api','action') AND at>?)", (after, after)).fetchone()[0]

    @synchronized
    def record_takeout_import(self, fingerprint, account_id, imported_at, observed_start, observed_end, unparsed_dates) -> int:
        """Record each upload's observed extent without claiming verified account-wide coverage.

        Watch-history files contain neither account proof nor an authoritative export interval.
        Even a fully parsed file may be filtered or partial; only a separately verified import
        may acquire coverage_start/end, account_verified and complete in a future verification flow.
        """
        cur = self.conn.execute(
            'INSERT INTO takeout_imports(account_id,fingerprint,imported_at,observed_start,'
            "observed_end,unparsed_dates) VALUES (?,?,?,?,?,?)",
            (account_id, fingerprint, imported_at, observed_start, observed_end, unparsed_dates))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def listening_day_counts(self, account_id=None):
        """Distinct account/recording/day counts, including independently dated evidence."""
        where,args = ('',()) if account_id is None else (' WHERE account_id=?',(account_id,))
        return {r[0]:r[1] for r in self.conn.execute(
            'SELECT identity_key,COUNT(*) FROM listening_days'+where+' GROUP BY identity_key',args)}

    @synchronized
    def event_play_counts(self, account_id=None):
        """Actual timestamped, non-generated events; coarse observations are excluded."""
        where,args = ('',()) if account_id is None else (' AND account_id=?',(account_id,))
        return {r[0]:r[1] for r in self.conn.execute(
            'SELECT identity_key,COUNT(*) FROM play_event_subjects WHERE COALESCE(is_generated,0)=0'+where+
            ' GROUP BY identity_key',args)}

    @synchronized
    def remove_listening_source(self, source, account_id=None):
        with self.conn:
            listening.delete_source(self.conn, source, account_id)

    @snapshot_read
    def spotify_receipt(self, account, uri, timestamp):
        row = self.conn.execute('SELECT event_id FROM spotify_history_listens '
            'WHERE account_id=? AND track_uri=? AND ended_at=?', (account, uri, timestamp)).fetchone()
        return dict(row) if row else None

    @synchronized
    def store_spotify_receipt(self, account, uri, timestamp, event, artist):
        self.conn.execute('INSERT INTO spotify_history_listens VALUES (?,?,?,?,?) '
            'ON CONFLICT(account_id,track_uri,ended_at) DO UPDATE SET '
            'event_id=excluded.event_id,artist_id=COALESCE(spotify_history_listens.artist_id,excluded.artist_id)',
            (account, uri, timestamp, event, artist))

    @synchronized
    def spotify_unmatched_artists(self, account_id):
        return {r['name']: r['plays'] for r in self.conn.execute(
            'SELECT a.name,COUNT(*) plays FROM spotify_history_listens s JOIN artists a ON a.id=s.artist_id '
            'WHERE s.account_id=? AND s.event_id IS NULL GROUP BY a.id', (account_id,))}

    @snapshot_read
    def spotify_history_stats(self):
        """Source attribution, counting shared events once rather than claiming ownership."""
        row = self.conn.execute(
            'SELECT COUNT(*) events,COUNT(DISTINCT pe.identity_key) tracks,MIN(pe.played_at) first_at,'
            'MAX(pe.played_at) last_at FROM play_event_subjects pe WHERE EXISTS ('
            "SELECT 1 FROM play_event_sources s WHERE s.event_id=pe.id AND s.source IN ('spotify','spotify_export'))"
        ).fetchone()
        days = self.conn.execute(
            'SELECT COUNT(*) FROM (SELECT DISTINCT account_id,identity_key,day FROM eligible_listening_evidence '
            "WHERE source IN ('spotify','spotify_export'))").fetchone()[0]
        artists = self.conn.execute(
            'SELECT COUNT(DISTINCT d.artist_id) FROM discovered_artists d JOIN discovery_artist_sources s '
            "ON s.artist_id=d.artist_id JOIN discovery_origins o ON o.id=s.origin_id "
            "WHERE o.kind IN ('spotify_export','spotify_model')").fetchone()[0]
        receipts = self.conn.execute('SELECT COUNT(*) FROM spotify_history_listens').fetchone()[0]
        unresolved = self.conn.execute('SELECT COUNT(*) FROM listening_observations '
            "WHERE day IS NULL AND source IN ('spotify','spotify_export')").fetchone()[0]
        return {**dict(row), 'days': days, 'artists': artists, 'listens': receipts,
                'unresolved_days': unresolved}

    @synchronized
    def delete_spotify_receipts(self):
        with self.conn.atomic():
            self.conn.execute('DELETE FROM main.spotify_history_listens')

    @synchronized
    def verified_imports(self):
        from yt_playlist.core.repair import _verified_imports
        return [dict(row) for row in _verified_imports(self.conn)]

    @synchronized
    def verified_imports_repaired(self):
        return worker_settings.read(self.conn, 'verified_imports_repaired')

    @synchronized
    def save_verified_imports_repaired(self, fingerprint):
        from yt_playlist.core.sync_state import save_history_progress
        with self.conn.atomic():
            save_history_progress(self.conn, 'verified_imports_repaired', fingerprint)

    @synchronized
    def contradicted_observations(self, windows, tolerance):
        from yt_playlist.core.repair import _phantom_rows
        rows = {}
        for account, lo, hi in windows:
            for row in _phantom_rows(self.conn, account, lo, hi, tolerance=tolerance):
                rows[row['id']] = dict(row)
        return list(rows.values())

    @synchronized
    def delete_observations(self, ids):
        with self.conn.atomic():
            self.conn.executemany('DELETE FROM listening_observations WHERE id=?', [(rid,) for rid in ids])
