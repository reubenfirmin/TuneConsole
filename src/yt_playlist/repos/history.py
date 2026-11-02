"""Listening days, exact plays, and retained source evidence."""
import datetime
import logging
import time
from collections import Counter, OrderedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from yt_playlist.core import artists, catalog, discovery_sources, entity_names, first_play, listening, listening_summaries
from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized, snapshot_read

logger = logging.getLogger(__name__)

_NOON = 43200            # compatibility date key, never an exact play timestamp
_EPOCH = datetime.date(1970, 1, 1)
_LIVE_DEDUP_S = 1800     # #75 same-track re-reports within this window merge (likeStatus updates);
                         # past it, the same track is a genuine replay and logs a new event
_UNDATED_BUCKETS = frozenset(('this week', 'last week'))
_BUCKET_WARNING_INTERVAL_S = 900
_BUCKET_WARNING_CACHE_SIZE = 128
_BUCKET_WARNING_SAMPLE_SIZE = 8


def _parse_played_date(played, taken_at, source_timezone=None):
    """Resolve YouTube's relative `played` bucket ('Today' / 'Yesterday' / 'Jun 25' / 'Jun 25, 2025') to
    an absolute LOCAL calendar date, stored at noon UTC, anchored on the sync time `taken_at` (#58).
    Use the browser's IANA timezone when supplied; older clients and explicit local callers use the
    machine's local timezone. The storage timestamp is only a date key, not the actual play time.
    Returns None when the bucket cannot be resolved.

    Because the bucket is relative, the SAME play reads 'Today' on one sync and 'Yesterday' the next.
    Both resolve to the same date, which is what makes play recording idempotent.

    An UNRESOLVABLE bucket returns None rather than falling back to the sync day. The old fallback was
    the phantom-play generator: get_history() returns YouTube's whole recently-played window (hundreds of
    rows) on every sync, so any row we could not date was stamped "played today", again, every single
    day. Measured on a real database: one sync wrote 230 history rows, of which 153 were tracks whose
    actual plays all happened on other days. It also hit every non-English locale at once, because the
    only date formats parsed here are English month names.

    None means "this row is in the window but we do not know when it was played", which is exactly the
    truth. The caller drops it. A play we cannot date is not lost: the extension records live plays with
    real timestamps (library/live_plays.py), and a dateable row still backfills.
    """
    timezone = None
    if source_timezone:
        try:
            timezone = ZoneInfo(source_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            logger.warning("history: invalid source timezone %r; using the machine's local timezone",
                           source_timezone)
    sync_day = datetime.datetime.fromtimestamp(taken_at, tz=timezone).date()
    p = (played or "").strip().lower()
    if p == "today":
        day = sync_day
    elif p == "yesterday":
        day = sync_day - datetime.timedelta(days=1)
    elif not p:
        return None                                      # no bucket at all: undateable
    elif p in _UNDATED_BUCKETS:
        return None                                      # known window, without an exact calendar day
    else:
        day = None
        s = (played or "").strip()
        for fmt, has_year in (("%b %d, %Y", True), ("%B %d, %Y", True), ("%b %d", False), ("%B %d", False)):
            try:
                if has_year:
                    d = datetime.datetime.strptime(s, fmt).date()
                else:                                    # pin the sync year explicitly (no 1900 default)
                    d = datetime.datetime.strptime(f"{s} {sync_day.year}", f"{fmt} %Y").date()
                    if d > sync_day:                     # e.g. "Dec 30" seen in early Jan -> previous year
                        d = d.replace(year=sync_day.year - 1)
            except (ValueError, TypeError, AttributeError):
                continue
            day = d
            break
        if day is None:                                  # localized month name, or a format we don't know
            # The sync owner reports counts, rather than warning for every row.
            return None
    return (day - _EPOCH).days * 86400 + _NOON


class HistoryRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        self._bucket_warnings = OrderedDict()

    def _log_undated_buckets(self, account_id, skipped):
        if not skipped:
            return
        unexpected = Counter({bucket: count for bucket, count in skipped.items()
                              if bucket and bucket not in _UNDATED_BUCKETS})
        logger.info('history sync: identity=%s skipped_undated=%d expected=%s missing=%d unexpected=%d',
                    account_id, sum(skipped.values()),
                    {bucket: skipped[bucket] for bucket in sorted(_UNDATED_BUCKETS) if skipped[bucket]},
                    skipped[''], sum(unexpected.values()))
        logger.debug('history undated buckets: identity=%s counts=%s', account_id, dict(skipped))
        now = time.monotonic()
        report = Counter()
        for bucket, count in unexpected.items():
            last = self._bucket_warnings.get(bucket)
            if last is None or now - last >= _BUCKET_WARNING_INTERVAL_S:
                report[bucket] = count
                self._bucket_warnings[bucket] = now
            if bucket in self._bucket_warnings:
                self._bucket_warnings.move_to_end(bucket)
            while len(self._bucket_warnings) > _BUCKET_WARNING_CACHE_SIZE:
                self._bucket_warnings.popitem(last=False)
        if report:
            # One bounded warning per sync, even if every row has a different label.
            sample = {bucket[:120]: count for bucket, count in report.most_common(_BUCKET_WARNING_SAMPLE_SIZE)}
            logger.warning('history: unexpected played buckets: identity=%s rows=%d buckets=%s '
                           'other_categories=%d; skipped without inventing play dates',
                           account_id, sum(report.values()), sample,
                           max(0, len(report) - _BUCKET_WARNING_SAMPLE_SIZE))

    @synchronized
    def record_history_plays(self, account_id, taken_at, items, *, source_timezone=None) -> int:
        """Retain dated sync evidence, returning newly observed account/recording days.

        Tuples contain YouTube date buckets; bare keys are independent coarse
        observations. Neither form creates an exact timestamped event.
        """
        with self.conn:
            policy = listening.calendar(self.conn,account_id,source_timezone)
            zone = source_timezone if listening._zone(source_timezone) else policy
            before = listening.day_count(self.conn,account_id)
            parsed, skipped, foreign = {}, Counter(), set()
            for item in items:
                bare = isinstance(item,str)
                key,played = (item,'today') if bare else item
                if not key:
                    continue
                bucket = (played or '').strip().lower()
                # A history response repeats a handful of labels across hundreds
                # of rows. Parse each once, scoped to this sync's time and calendar.
                if bucket not in parsed:
                    parsed[bucket] = _parse_played_date(played,taken_at,zone)
                stamp = parsed[bucket]
                if stamp is None:
                    skipped[bucket] += 1
                    continue
                day = datetime.datetime.fromtimestamp(stamp,datetime.timezone.utc).date().isoformat()
                scope = listening.observe_day(self.conn,account_id,key,day,
                    'independent' if bare else 'ytm_history',zone,taken_at,reconcile=False)
                if scope is not None:
                    foreign.add(scope)
            listening.reconcile_foreign_days(self.conn,account_id,observations=foreign)
            first_play.invalidate(self.conn)
            added = max(0,listening.day_count(self.conn,account_id)-before)
        self._log_undated_buckets(account_id, skipped)
        return added

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
    def get_recent_history_keys(self, since_ts) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT identity_key FROM listening_activity WHERE at>=?",
            (since_ts,)).fetchall()
        return {r["identity_key"] for r in rows}

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
    def recent_play_counts(self, limit) -> dict:
        """Recent activity counts: exact repeats plus coarse days without an exact event."""
        rows = self.conn.execute(
            "SELECT identity_key k, COUNT(*) c FROM ("
            "  SELECT identity_key FROM listening_activity "
            "  ORDER BY at DESC,account_id,identity_key,event_id DESC LIMIT ?"
            ") GROUP BY identity_key", (limit,)).fetchall()
        return {r["k"]: r["c"] for r in rows}

    @synchronized
    def recent_keys_ordered(self, since_ts, limit=None) -> list[str]:
        """Account keys played at/after since_ts, most recent activity first.
        For the recent-mood centroid, which wants the latest plays, not an arbitrary subset of a set."""
        rows = self.conn.execute(
            "SELECT identity_key k,MAX(at) last FROM listening_activity WHERE at>=? "
            "GROUP BY identity_key ORDER BY last DESC", (since_ts,)).fetchall()
        keys = [r["k"] for r in rows]
        return keys[:limit] if limit else keys

    @synchronized
    def recent_plays_with_ts(self, limit=None, exclude_list_ids=None) -> list:
        """Newest activity per key, retaining exact times when available.

        Excluded playlist events suppress only their account/day sync shadows.
        Independent and unknown coarse evidence survives; adjacent dates are not guessed.
        """
        if exclude_list_ids:
            excl = list(exclude_list_ids)
            ph = ",".join("?" * len(excl))
            q = ("WITH kept AS (SELECT pe.account_id,pe.identity_key,pe.played_at,c.day "
                 "FROM play_event_subjects pe JOIN play_event_calendar c ON c.event_id=pe.id "
                 f"WHERE COALESCE(pe.is_generated,0)=0 AND (pe.playlist_ytm_id IS NULL OR pe.playlist_ytm_id NOT IN ({ph}))"
                 ") "
                 "SELECT k, MAX(ts) ts FROM ("
                 "  SELECT identity_key k, played_at ts FROM kept"
                 "  UNION ALL"
                 "  SELECT e.identity_key k,CAST(strftime('%s',e.day||' 12:00:00') AS REAL) ts"
                 "  FROM eligible_listening_evidence e WHERE e.event_id IS NULL"
                 "  AND NOT EXISTS (SELECT 1 FROM kept k WHERE k.account_id=e.account_id "
                 "    AND k.identity_key=e.identity_key AND k.day=e.day)"
                 "  AND (e.source<>'ytm_history' OR NOT EXISTS ("
                 "    SELECT 1 FROM play_event_subjects pe JOIN play_event_calendar c ON c.event_id=pe.id"
                 "    WHERE pe.account_id=e.account_id AND pe.identity_key=e.identity_key"
                 f"    AND pe.playlist_ytm_id IN ({ph})"
                 "    AND c.day=e.day))"
                 ") GROUP BY k ORDER BY ts DESC")
            params: tuple = (*excl, *excl)
        else:
            q = "SELECT identity_key k,MAX(at) ts FROM listening_activity GROUP BY identity_key ORDER BY ts DESC"
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
    def record_play_event(self, account_id, identity_key, video_id, played_at,
                          playlist_ytm_id=None, like_status=None, *, source_timezone=None,
                          context_choice=False) -> bool:
        """#75 Persist one live now-playing report from the extension. The content script re-reports
        the SAME track when its likeStatus changes, so a report matching the identity's latest event
        (same key, within _LIVE_DEDUP_S) updates that row (like_status; playlist id only if missing)
        instead of logging a second play. Snapshot generated policy with the playlist context so
        promotion cannot rewrite a play's provenance. A selected full-record context is likewise
        quarantined from organic per-track learning. Returns True when a NEW play was recorded."""
        is_generated = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM quarantined_playlists q "
            "JOIN playlist_scopes s ON s.id=q.playlist_scope_id WHERE q.name=? "
            "AND (s.provider_playlist_id=? OR 'RDAMPL' || s.provider_playlist_id=?) "
            "AND (s.account_id IS NULL OR s.account_id=?))",
            (GENERATED_GROUP, playlist_ytm_id, playlist_ytm_id, account_id)).fetchone()[0]
        transient_radio = (playlist_ytm_id or '').removeprefix('RDAMPL') in self._store.transient_radio_playlists
        is_generated = int(bool(is_generated or context_choice or transient_radio))
        with self.conn:
            listening.calendar(self.conn,account_id,source_timezone)
            subject = catalog.play_key(self.conn,identity_key,video_id)
            row = self._matching_event(account_id,subject,video_id,played_at)
            if row is None:
                latest = self.conn.execute(
                    'SELECT * FROM play_event_subjects WHERE account_id=? ORDER BY played_at DESC,id DESC LIMIT 1',
                    (account_id,)).fetchone()
                if (latest and latest['identity_key']==subject
                        and (not video_id or not latest['video_id'] or video_id==latest['video_id'])
                        and 0 <= played_at-latest['played_at'] < _LIVE_DEDUP_S
                        and self.conn.execute("SELECT 1 FROM play_event_sources WHERE event_id=? AND source='live'",
                                              (latest['id'],)).fetchone()):
                    row = latest
            new = row is None
            if new:
                event_id = listening.append_event(self.conn,account_id,identity_key,played_at,
                    video_id=video_id,playlist_ytm_id=playlist_ytm_id,like_status=like_status,is_generated=is_generated)
                timestamp = played_at
            else:
                event_id,timestamp = row['id'],row['played_at']
                typed = listening_summaries.available(self.conn)
                membership_sql = ('membership_ref_id=CASE WHEN playlist_ytm_id IS NULL AND ? IS NOT NULL '
                                  'THEN ? ELSE membership_ref_id END,') if typed else ''
                membership_args = (playlist_ytm_id, listening.membership_reference(
                    self.conn, account_id, playlist_ytm_id)) if typed else ()
                self.conn.execute(
                    'UPDATE main.play_events SET like_status=COALESCE(?,like_status),'
                    "video_id=COALESCE(NULLIF(trim(video_id),''),?)," + membership_sql +
                    'is_generated=CASE WHEN playlist_ytm_id IS NULL AND ? IS NOT NULL '
                    'THEN ? ELSE is_generated END,playlist_ytm_id=COALESCE(playlist_ytm_id,?) WHERE id=?',
                    (like_status,video_id,*membership_args,playlist_ytm_id,is_generated,playlist_ytm_id,event_id))
            listening.project_event(self.conn,event_id,account_id,timestamp,source='live')
            listening.reconcile_foreign_days(self.conn,account_id,events={(subject,timestamp)})
            first_play.invalidate(self.conn)
            return new

    def _matching_event(self, account_id, subject, video_id, timestamp):
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?',(subject,)).fetchone()
        subject_column = 'unresolved_subject_key' if listening_summaries.available(self.conn) else 'identity_key'
        scope,args = ('recording_id=?',(recording[0],)) if recording else (
            f'recording_id IS NULL AND {subject_column}=?',(subject,))
        return self.conn.execute(
            f'SELECT * FROM play_event_subjects WHERE account_id=? AND {scope} '
            "AND (NULLIF(trim(video_id),'') IS NULL OR NULLIF(trim(?),'') IS NULL OR video_id=?) "
            'AND played_at BETWEEN ?-2.0 AND ?+2.0 ORDER BY ABS(played_at-?),id LIMIT 1',
            (account_id,*args,video_id,video_id,timestamp,timestamp,timestamp)).fetchone()

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
    def import_plays(self, account_id, plays, *, source='takeout', source_timezone=None) -> int:
        """Compatibility entry point for timestamped imports; return new listening days."""
        days,_ = self.import_listening_events(account_id,((k,None,ts) for k,ts in plays),
                                               source=source,source_timezone=source_timezone)
        return days

    @synchronized
    def record_takeout_import(self, fingerprint, account_id, imported_at, parsed, unparsed_dates) -> int:
        """Record each upload's observed extent without claiming verified account-wide coverage.

        Watch-history files contain neither account proof nor an authoritative export interval.
        Even a fully parsed file may be filtered or partial; only a separately verified import
        may acquire coverage_start/end, account_verified and complete in a future verification flow.
        """
        imports = entity_names.owner(self.conn, 'takeout_imports')
        cur = self.conn.execute(
            f'INSERT INTO {imports.table}({imports.column("account_id")},fingerprint,imported_at,observed_start,'
            "observed_end,unparsed_dates) VALUES (?,?,?,?,?,?)",
            (account_id, fingerprint, imported_at, parsed[0]['ts'] if parsed else None,
             parsed[-1]['ts'] if parsed else None, unparsed_dates))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def import_play_events(self, account_id, rows, source="takeout") -> int:
        """Compatibility entry point returning new exact events, not listening days."""
        _,events = self.import_listening_events(account_id,rows,source=source)
        return events

    @synchronized
    def import_listening_events(self, account_id, rows, *, source='takeout', source_timezone=None):
        """Atomically reconcile [(key, video, timestamp)] and retain every source.

        HTML/JSON timestamp rounding and live/import overlap deduplicate within
        two seconds. Different known uploads and genuine later plays remain events.
        Returns (new account/recording days, new exact events).
        """
        with self.conn:
            listening.calendar(self.conn,account_id,source_timezone)
            before = listening.day_count(self.conn,account_id)
            added = 0
            affected = set()
            for key,video,timestamp in rows:
                if not key:
                    continue
                subject = catalog.play_key(self.conn,key,video)
                row = self._matching_event(account_id,subject,video,timestamp)
                if row is None:
                    event_id = listening.append_event(self.conn,account_id,key,timestamp,video_id=video)
                    added += 1
                else:
                    event_id,timestamp = row['id'],row['played_at']
                    self.conn.execute("UPDATE main.play_events SET video_id=COALESCE(NULLIF(trim(video_id),''),?) WHERE id=?",
                                      (video,event_id))
                listening.project_event(self.conn,event_id,account_id,timestamp,source=source)
                affected.add((subject,timestamp))
            listening.reconcile_foreign_days(self.conn,account_id,events=affected)
            first_play.invalidate(self.conn)
            return max(0,listening.day_count(self.conn,account_id)-before),added

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
            listening.remove_source(self.conn,source,account_id)

    @synchronized
    def import_spotify_listens(self, account_id, rows):
        """Retain stable Spotify occurrence IDs, independent of changing display metadata.

        Rows contain (URI, end timestamp, unmatched artist name, matched key).
        Unmatched receipts let overlapping files accumulate artist interest once;
        they can be matched later if the user adds that music to the library.
        """
        with self.conn:
            listening.calendar(self.conn,account_id)
            receipts = entity_names.owner(self.conn, 'spotify_history_listens')
            before = listening.day_count(self.conn,account_id)
            added = 0
            artist_ids = {}
            listens_added = 0
            affected = set()
            for uri, timestamp, artist, key in rows:
                receipt = self.conn.execute(
                    'SELECT event_id FROM spotify_history_listens WHERE account_id=? AND track_uri=? AND ended_at=?',
                    (account_id,uri,timestamp)).fetchone()
                if receipt and (receipt['event_id'] is not None or not key):
                    continue
                listens_added += receipt is None
                event_id, aid = None, None
                if not key:
                    if artist not in artist_ids:
                        artist_ids[artist] = artists.state_id(self.conn,artist)
                    aid = artist_ids[artist]
                if key:
                    subject = catalog.play_key(self.conn,key,None)
                    event = self._matching_event(account_id,subject,None,timestamp)
                    if event is None:
                        event_id = listening.append_event(self.conn,account_id,key,timestamp)
                        added += 1
                        event_time = timestamp
                    else:
                        event_id, event_time = event['id'], event['played_at']
                    listening.project_event(self.conn,event_id,account_id,event_time,source='spotify_export')
                    affected.add((subject,event_time))
                self.conn.execute(f'INSERT INTO {receipts.table} VALUES (?,?,?,?,?) '
                    f'ON CONFLICT({receipts.column("account_id")},track_uri,ended_at) DO UPDATE SET '
                    'event_id=excluded.event_id,artist_id=COALESCE(spotify_history_listens.artist_id,excluded.artist_id)',
                    (account_id,uri,timestamp,event_id,aid))
            listening.reconcile_foreign_days(self.conn,account_id,events=affected)
            first_play.invalidate(self.conn)
            return max(0,listening.day_count(self.conn,account_id)-before), added, listens_added

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
    def remove_spotify_history(self):
        """Remove Spotify evidence and invalidate learned caches in one transaction.

        Shared events, independent suggestions, library curation and explicit user
        feedback survive. Model rollback snapshots must not restore deleted evidence.
        The caller serializes imports/model work and schedules the fresh build.
        """
        with self.conn:
            before = self.spotify_history_stats()
            self.conn.execute('DELETE FROM main.spotify_history_listens')
            for source in ('spotify_export','spotify'):
                listening.remove_source(self.conn,source)
            artists_removed = discovery_sources.remove_spotify_artists(self.conn)
            self.conn.execute("DELETE FROM recording_embeddings WHERE space='collaborative'")
            from yt_playlist.core import embedding_files
            if embedding_files.available(self.conn):
                self.conn.execute("DELETE FROM recording_embedding_files WHERE space='collaborative'")
                self.conn.execute('DELETE FROM artist_embedding_files')
            from yt_playlist.core import group_preparation_state, model_selection_heads, model_state
            if model_selection_heads.available(self.conn):
                model_selection_heads.put(self.conn,{key:None for key in model_selection_heads.KEYS})
            if self._store._model_comparison_state:
                from yt_playlist.core import model_comparison_state
                model_comparison_state.clear(self.conn)
            if self._store._model_projections:
                from yt_playlist.core import model_projections
                model_projections.clear(self.conn)
            from yt_playlist.core import recommendation_setting_outputs
            if recommendation_setting_outputs.available(self.conn):
                recommendation_setting_outputs.clear(self.conn)
            for table in ('rec_proposals','artist_embeddings',
                          'rec_model_previous_vectors','rec_model_forecasts','rec_model_selection_runs'):
                self.conn.execute(f'DELETE FROM {table}')
            from yt_playlist.core.recommendation_invalidation import clear_display, clear_findings
            clear_display(self.conn)
            clear_findings(self.conn)
            if model_state.available(self.conn):
                model_state.put(self.conn,{'rec_artist_content_model':None,'rec_fit_inputs':None,'rec_artist_fit_inputs':None})
                group_preparation_state.put(self.conn,{'rec_mode_fit_inputs':None})
            self.conn.execute("DELETE FROM settings WHERE key IN ('spotify_imported_at','spotify_import_report',"
                              "'rec_selection_active_run','rec_selection_display_run','rec_selection_restore',"
                              "'rec_artist_content_model','rec_autotune_result','trend_spotlight_review_month',"
                              "'rec_fit_inputs','rec_artist_fit_inputs','rec_mode_fit_inputs','rec_refresh_version')")
            return {**before, 'artists_removed': artists_removed}
