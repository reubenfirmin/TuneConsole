"""Listening days, exact plays, and retained source evidence."""
from contextlib import contextmanager
from yt_playlist.core import catalog_keys, entity_names, first_play, listening, listening_summaries
from yt_playlist.core.preparation import listening_views
from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class HistoryRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock, self.conn.atomic():
            yield

    @synchronized
    def day_count(self, account):
        return listening.day_count(self.conn, account)

    @synchronized
    def invalidate_first_play(self):
        with self.conn.atomic(join=True):
            first_play.invalidate(self.conn)

    @snapshot_read
    def calendar_events(self, account):
        return [dict(row) for row in self.conn.execute('SELECT id,played_at FROM play_events WHERE account_id=?', (account,))]

    @snapshot_read
    def calendar_accounts(self, account=None):
        return [row[0] for row in self.conn.execute('SELECT account_id FROM listening_calendars' +
            ('' if account is None else ' WHERE account_id=?'), () if account is None else (account,))]

    @synchronized
    def calendar_facts(self, account_id):
        owner = entity_names.owner(self.conn, 'listening_calendars')
        row = self.conn.execute(f'SELECT timezone,confirmed FROM {owner.table} '
            f'WHERE {owner.column("account_id")}=?', (account_id,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def set_calendar(self, account_id, name, *, confirmed, event_days=None):
        owner = entity_names.owner(self.conn, 'listening_calendars')
        with self.conn.atomic():
            self.conn.execute(f'INSERT INTO {owner.table} VALUES (?,?,?) '
                f'ON CONFLICT({owner.column("account_id")}) DO UPDATE SET '
                'timezone=excluded.timezone,confirmed=excluded.confirmed', (account_id, name, int(confirmed)))
            if event_days is not None:
                self.conn.executemany('INSERT INTO play_event_calendar(event_id,day,timezone) VALUES (?,?,?) '
                    'ON CONFLICT(event_id) DO UPDATE SET day=excluded.day,timezone=excluded.timezone', event_days)
                observations = entity_names.owner(self.conn, 'listening_observations')
                self.conn.execute(f'UPDATE {observations.table} SET day=CASE WHEN timezone=? THEN source_day END '
                    f"WHERE {observations.column('account_id')}=? AND precision='calendar_day'", (name, account_id))
                first_play.invalidate(self.conn)

    @synchronized
    def record_days(self, account_id, taken_at, observations, timezone):
        """Persist prepared subjects and calendar-day projections."""
        with self.conn.atomic():
            owner = entity_names.owner(self.conn, 'listening_observations')
            typed = listening_summaries.available(self.conn)
            column = 'unresolved_subject_key' if typed else 'identity_key'
            for key, subject, recording, source_day, day, source in observations:
                self.conn.execute(f'INSERT INTO {owner.table}({owner.column("account_id")},raw_identity_key,{column},recording_id,'
                    'source_day,day,source,timezone,precision,first_observed_at,last_observed_at) '
                    "VALUES (?,?,?,?,?,?,?,?,'calendar_day',?,?) "
                    f'ON CONFLICT({owner.column("account_id")},raw_identity_key,source_day,source,timezone) DO UPDATE SET '
                    'first_observed_at=min(first_observed_at,excluded.first_observed_at),'
                    'last_observed_at=max(last_observed_at,excluded.last_observed_at)',
                    (account_id, key, None if typed and recording else subject, recording,
                     source_day, day, source, timezone, taken_at, taken_at))
            first_play.invalidate(self.conn)

    @synchronized
    def reset_play_history(self, account_id=None) -> None:
        """Clear coarse captures for a fresh sync, retaining the exact-event ledger.

        Scoped to one account, or all when None. Exact plays continue to contribute
        listening days; a history-window reset does not delete timestamped evidence.
        """
        with self.conn:
            observations = entity_names.owner(self.conn, 'listening_observations')
            receipts = entity_names.owner(self.conn, 'listening_capture_receipts')
            if account_id is None:
                self.conn.execute(f'DELETE FROM {observations.table}')
                self.conn.execute(f'DELETE FROM {receipts.table}')
            else:
                self.conn.execute(f'DELETE FROM {observations.table} WHERE {observations.column("account_id")}=?',(account_id,))
                self.conn.execute(f'DELETE FROM {receipts.table} WHERE {receipts.column("account_id")}=?', (account_id,))
            first_play.invalidate(self.conn)

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
        native = self.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='listening_summary_state'").fetchone()
        prefix, names = listening_views.materialized() if native else ('', {})
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
        exact = self.conn.execute('SELECT r.id,r.identity_key FROM youtube_items y '
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
        events = entity_names.owner(self.conn, 'play_events')
        if not listening_summaries.available(self.conn):
            # Installed triggers retain the original key and resolve its subject.
            return self.conn.execute(f'INSERT INTO {events.table}({events.column("account_id")},identity_key,'
                'video_id,played_at,playlist_ytm_id,like_status,is_generated) VALUES (?,?,?,?,?,?,?)',
                (account,key,video_id,timestamp,playlist_ytm_id,like_status,is_generated)).lastrowid
        return self.conn.execute(f'INSERT INTO {events.table}({events.column("account_id")},raw_identity_key,'
            'recording_id,unresolved_subject_key,video_id,played_at,playlist_ytm_id,like_status,is_generated,membership_ref_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)', (account,key,recording,None if recording else subject,video_id,timestamp,
                playlist_ytm_id,like_status,is_generated,listening.membership_reference(self.conn,account,playlist_ytm_id))).lastrowid

    @synchronized
    def update_event(self, event, account, video, playlist, rating, generated):
        typed = listening_summaries.available(self.conn)
        membership_sql = ('membership_ref_id=CASE WHEN playlist_ytm_id IS NULL AND ? IS NOT NULL '
                          'THEN ? ELSE membership_ref_id END,') if typed else ''
        membership_args = (playlist, listening.membership_reference(self.conn, account, playlist)) if typed else ()
        self.conn.execute('UPDATE main.play_events SET like_status=COALESCE(?,like_status),'
            "video_id=COALESCE(NULLIF(trim(video_id),''),?)," + membership_sql +
            'is_generated=CASE WHEN playlist_ytm_id IS NULL AND ? IS NOT NULL '
            'THEN ? ELSE is_generated END,playlist_ytm_id=COALESCE(playlist_ytm_id,?) WHERE id=?',
            (rating, video, *membership_args, playlist, generated, playlist, event))

    @synchronized
    def project_event(self, event, day, timezone, source):
        self.conn.execute('INSERT INTO play_event_calendar(event_id,day,timezone) VALUES (?,?,?) '
            'ON CONFLICT(event_id) DO UPDATE SET day=excluded.day,timezone=excluded.timezone', (event, day, timezone))
        self.conn.execute('INSERT OR IGNORE INTO play_event_sources VALUES (?,?)', (event, source))

    @snapshot_read
    def matching_event(self, account_id, subject, video_id, timestamp, tolerance):
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?',(subject,)).fetchone()
        subject_column = 'unresolved_subject_key' if listening_summaries.available(self.conn) else 'identity_key'
        scope,args = ('recording_id=?',(recording[0],)) if recording else (
            f'recording_id IS NULL AND {subject_column}=?',(subject,))
        return self.conn.execute(
            f'SELECT * FROM play_event_subjects WHERE account_id=? AND {scope} '
            "AND (NULLIF(trim(video_id),'') IS NULL OR NULLIF(trim(?),'') IS NULL OR video_id=?) "
            'AND played_at BETWEEN ?-? AND ?+? ORDER BY ABS(played_at-?),id LIMIT 1',
            (account_id,*args,video_id,video_id,timestamp,tolerance,timestamp,tolerance,timestamp)).fetchone()

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

    @synchronized
    def play_events_since(self, since_ts, exclude_list_ids=None, *, taste_only=False) -> list[dict]:
        """#75 Play events at/after since_ts, oldest first.

        `exclude_list_ids`, when given, drops rows whose playlist_ytm_id is in that set (#93v2: a
        caller reading recent plays as mood/taste EVIDENCE passes the radio playlists here, so the
        radio's own machine-queued plays cannot feed back into the model that steers the radio - see
        rec/transient.py's radio_list_ids for the loop this closes). Rows with a NULL playlist_ytm_id
        are ALWAYS kept regardless of the exclusion set: no provenance means the play was not
        attributed to any machine-queued playlist (a direct search/album play, or a pre-provenance
        row), so it is user-driven and must still count."""
        # Taste consumers additionally retain generated provenance after a playlist is
        # removed. Descriptive callers keep the complete event stream by default.
        evidence = ("AND COALESCE(is_generated,0)=0 AND NOT EXISTS "
                    "(SELECT 1 FROM quarantined_playlists q WHERE q.playlist_scope_id=playlist_ytm_id) "
                    if taste_only else "")
        if exclude_list_ids:
            excl = list(exclude_list_ids)
            placeholders = ",".join("?" * len(excl))
            q = ("SELECT account_id, identity_key, video_id, played_at, playlist_ytm_id, like_status "
                 f"FROM play_event_subjects WHERE played_at>=? "
                 f"AND (playlist_ytm_id IS NULL OR playlist_ytm_id NOT IN ({placeholders})) "
                 + evidence + "ORDER BY played_at, id")
            rows = self.conn.execute(q, (since_ts, *excl)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT account_id, identity_key, video_id, played_at, playlist_ytm_id, like_status "
                "FROM play_event_subjects WHERE played_at>=? " + evidence + "ORDER BY played_at, id", (since_ts,)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def record_takeout_import(self, fingerprint, account_id, imported_at, observed_start, observed_end, unparsed_dates) -> int:
        """Record each upload's observed extent without claiming verified account-wide coverage.

        Watch-history files contain neither account proof nor an authoritative export interval.
        Even a fully parsed file may be filtered or partial; only a separately verified import
        may acquire coverage_start/end, account_verified and complete in a future verification flow.
        """
        imports = entity_names.owner(self.conn, 'takeout_imports')
        cur = self.conn.execute(
            f'INSERT INTO {imports.table}({imports.column("account_id")},fingerprint,imported_at,observed_start,'
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
            observations = entity_names.owner(self.conn, 'listening_observations')
            events = entity_names.owner(self.conn, 'play_events')
            scope = '' if account_id is None else f' AND {observations.column("account_id")}=?'
            args = (source,) if account_id is None else (source, account_id)
            self.conn.execute(f'DELETE FROM {observations.table} WHERE source=?' + scope, args)
            self.conn.execute('DELETE FROM play_event_sources WHERE source=? AND event_id IN '
                f'(SELECT id FROM {events.table} WHERE 1=1' + scope + ')', args)
            self.conn.execute(f'DELETE FROM {events.table} WHERE NOT EXISTS '
                '(SELECT 1 FROM play_event_sources s WHERE s.event_id=play_events.id)' + scope,
                () if account_id is None else (account_id,))
            first_play.invalidate(self.conn)

    @snapshot_read
    def spotify_receipt(self, account, uri, timestamp):
        row = self.conn.execute('SELECT event_id FROM spotify_history_listens '
            'WHERE account_id=? AND track_uri=? AND ended_at=?', (account, uri, timestamp)).fetchone()
        return dict(row) if row else None

    @synchronized
    def store_spotify_receipt(self, account, uri, timestamp, event, artist):
        receipts = entity_names.owner(self.conn, 'spotify_history_listens')
        self.conn.execute(f'INSERT INTO {receipts.table} VALUES (?,?,?,?,?) '
            f'ON CONFLICT({receipts.column("account_id")},track_uri,ended_at) DO UPDATE SET '
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
            first_play.invalidate(self.conn)
