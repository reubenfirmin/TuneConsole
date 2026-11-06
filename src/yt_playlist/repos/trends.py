"""Listening trend reads and publication of disposable canonical subject summaries."""
from datetime import datetime
from zoneinfo import ZoneInfo

from yt_playlist.repos.base import Repo, synchronized, snapshot_read


class TrendsRepo(Repo):
    # -- the play ledger -----------------------------------------------------------------------
    # play_events is one row per REAL play, with a real timestamp and (for live-captured rows) the
    # playlist it was played from. Generation policy is retained on each event;
    # dated sync shadows are reconciled separately in listening_days.
    #
    # Everything below returns the same shapes the history queries return, so the rollup consumes
    # either. The difference is what a count means: a real event here, an account listening day there.

    def _generated_filter(self, exclude_generated):
        """(sql_fragment, args) restricting play_events (aliased `pe`) to non-generated plays.

        Un-decorated so the @synchronized public methods can reuse it. A NULL playlist_ytm_id (every
        Takeout-backfilled row) cannot be proven generated, so it is kept.
        """
        if not exclude_generated:
            return "", []
        return " AND COALESCE(pe.is_generated,0)=0", []

    @synchronized
    def catalog_artists(self) -> set[str]:
        """Artists with at least one track in a playlist the user owns (any group except Generated).

        This is what separates "new to you" from "new to our logs". The play ledger only begins when
        the extension or a Takeout export begins, so an artist you have loved for a decade looks
        brand-new the first time we happen to see a play. Catalog membership is the prior: if a track
        of theirs already sits in one of your playlists, you knew them.

        Generated playlists are excluded, per the GENERATED_GROUP quarantine (repos/base.py): the app
        putting an artist in front of you is not you knowing them. A generated playlist you PROMOTE out
        of that group becomes a real playlist and its artists become catalog, which is exactly the
        graduation semantics base.py describes.

        Approximation, stated plainly: playlist_tracks carries no timestamp, so a track you discover
        today and save tomorrow reads as catalog for every past week too. That biases discovery DOWN
        (a real discovery can be missed), never up (a familiar artist is never called new).
        """
        rows = self.conn.execute(
            "SELECT DISTINCT '@artist:'||c.artist_id a FROM playlist_tracks pt "
            "JOIN catalog_entries t ON t.id = pt.track_id "
            "JOIN recording_listening_artists c ON c.recording_id=t.recording_id "
            "JOIN playlists p ON p.id = pt.playlist_id "
            "WHERE p.playlist_scope_id NOT IN "
            "  (SELECT playlist_scope_id FROM quarantined_playlists)").fetchall()
        return {r["a"] for r in rows}

    @synchronized
    def has_play_ledger(self) -> bool:
        """True when play_events holds anything. False for a fresh install with no extension and no
        Takeout import, where the coarse history day model is still the only evidence of listening."""
        return self.conn.execute("SELECT 1 FROM play_events LIMIT 1").fetchone() is not None

    @snapshot_read
    def ledger_day_counts(self, exclude_generated=True) -> list:
        """[(day, identity_key, plays)] from the play ledger: one row per (account day, track), plays =
        the number of REAL plays that day. Same shape as play_day_counts(), so the rollup consumes it
        unchanged, but the number means events rather than distinct account listening days.

        Identities are merged, not distinguished. The history day model keyed its dedupe on
        (identity, date), which double-counted a track played on two accounts the same day.
        """
        frag, args = self._generated_filter(exclude_generated)
        where = f"WHERE 1=1{frag}" if frag else ""
        rows = self.conn.execute(
            f"SELECT CAST(julianday(c.day)-julianday('1970-01-01') AS INTEGER) day,pe.identity_key k,COUNT(*) c "
            f"FROM play_event_subjects pe JOIN play_event_calendar c ON c.event_id=pe.id {where} GROUP BY c.day,k", args).fetchall()
        return [(r["day"], r["k"], r["c"]) for r in rows]

    @snapshot_read
    def ledger_track_plays(self, since, until, exclude_generated=True, *, calendar=False) -> dict:
        """{identity_key: plays} real plays in [since, until). Replaces month_track_plays, whose
        Counting coarse observations would mistake lingering history windows for repeat plays."""
        frag, args = self._generated_filter(exclude_generated)
        join, period = self._period(calendar)
        rows = self.conn.execute(
            f"SELECT pe.identity_key k, COUNT(*) c FROM play_event_subjects pe {join} "
            f"WHERE {period}{frag} GROUP BY pe.identity_key",
            [since, until, *args]).fetchall()
        return {r["k"]: r["c"] for r in rows}

    @synchronized
    def track_audio(self) -> dict:
        """{identity_key: {energy, bpm, dance}} averaged per key over its track rows. Keys with no
        acoustic data at all are omitted (each field can still be None). Feeds the recap personality's
        energy axis; the caller falls back to genre priors where a played key is absent here."""
        rows = self.conn.execute(
            "SELECT identity_key k, AVG(energy) e, AVG(bpm) b, AVG(danceability) d FROM tracks "
            "WHERE energy IS NOT NULL OR bpm IS NOT NULL OR danceability IS NOT NULL "
            "GROUP BY identity_key").fetchall()
        return {r["k"]: {"energy": r["e"], "bpm": r["b"], "dance": r["d"]} for r in rows}

    @snapshot_read
    def play_hours(self, since, until, exclude_generated=True, *, calendar=False) -> list:
        """24 hour buckets of real plays. Calendar mode uses retained account-local days
        and hours; the default uses UTC instants. Feeds the recap personality's rhythm axis."""
        frag, args = self._generated_filter(exclude_generated)
        hours = [0] * 24
        if calendar:
            join, period = self._period(True)
            for row in self.conn.execute(
                    f"SELECT pe.played_at,c.timezone FROM play_events pe {join} WHERE {period}{frag}",
                    [since, until, *args]):
                hours[datetime.fromtimestamp(row[0], ZoneInfo(row[1])).hour] += 1
            return hours
        for r in self.conn.execute(
                f"SELECT CAST(strftime('%H', pe.played_at, 'unixepoch') AS INTEGER) h, COUNT(*) c "
                f"FROM play_events pe WHERE pe.played_at >= ? AND pe.played_at < ?{frag} GROUP BY h",
                [since, until, *args]).fetchall():
            if r["h"] is not None:
                hours[r["h"]] = r["c"]
        return hours

    @snapshot_read
    def ledger_artist_plays(self, since, until, exclude_generated=True, *, calendar=False) -> dict:
        """{artist: plays} real plays in [since, until), artist resolved through tracks. Tracks with a
        blank or missing artist are skipped. Replaces charts.listen_distribution('artist', ...) inside
        the Trends rollup only; the charts/ticker consumers keep their own day-model semantics."""
        frag, args = self._generated_filter(exclude_generated)
        join, period = self._period(calendar)
        rows = self.conn.execute(
            "SELECT '@artist:'||n.artist_id artist,COUNT(*) c FROM play_events pe "
            "JOIN recording_listening_artists n ON n.recording_id=pe.recording_id "
            f"{join} WHERE {period}{frag} GROUP BY n.artist_id",
            [since, until, *args]).fetchall()
        return {r["artist"]: r["c"] for r in rows}

    @staticmethod
    def _period(calendar):
        # UTC timestamps label the requested calendar dates, rather than shifting all
        # accounts by one offset. Each retained event projection owns its listening day.
        if calendar:
            return ("JOIN play_event_calendar c ON c.event_id=pe.id",
                    "c.day>=date(?,'unixepoch') AND c.day<date(?,'unixepoch')")
        return "", "pe.played_at>=? AND pe.played_at<?"

    # -- the reconciled daily model ------------------------------------------------------------
    def _replace_first_play(self):
        from yt_playlist.core import first_play, listening_summaries
        if not listening_summaries.available(self.conn):
            return first_play.rebuild_predecessor(self.conn)
        return listening_summaries.rebuild(self.conn)

    @synchronized
    def prepare_first_play(self):
        """Build the index privately on a maintenance reader, without changing published data.

        The temporary table shadows the serving index for this reader's subsequent rollup queries.
        Its writes stay private even though the source connection is read-only.
        """
        from yt_playlist.core import first_play, listening_summaries
        if not listening_summaries.available(self.conn):
            return first_play.rebuild_predecessor(self.conn, private=True)
        return listening_summaries.rebuild(self.conn,private=True)

    def _first_play_settings(self, imported):
        self.conn.execute("INSERT INTO settings(key,value) VALUES ('trend_first_play_source','canonical_listening') "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        self.conn.execute("DELETE FROM settings WHERE key='trend_first_play_watermark'")
        if imported is None:
            self.conn.execute("DELETE FROM settings WHERE key='trend_first_play_takeout_seen'")
        else:
            self.conn.execute("INSERT INTO settings(key,value) VALUES ('trend_first_play_takeout_seen',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (imported,))

    @synchronized
    def publish_rollup(self, rows, encoded_payload, now, imported):
        """Write prepared output inside ReadSnapshot.publish's checked transaction. No commit."""
        from yt_playlist.core import first_play, listening_summaries
        if listening_summaries.available(self.conn):
            listening_summaries.publish(self.conn,rows)
        else:
            first_play.publish_predecessor(self.conn, rows)
        self._first_play_settings(imported)
        if self._store._home_display_files:
            self._store.rec_proposals.publish_prepared_home(encoded_payload, now, surface='trend_rollups')
            return
        self.conn.execute(
            "INSERT INTO rec_proposals(surface,payload,built_at) VALUES ('trend_rollups',?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at",
            (encoded_payload, now))

    @synchronized
    def clear_first_play(self) -> None:
        from yt_playlist.core.first_play import invalidate

        with self.conn:
            invalidate(self.conn)

    @synchronized
    def refresh_first_play(self):
        """Fold observations and publish track/artist indexes plus checkpoints in one transaction.

        An exact ledger cannot erase older coarse evidence. Late evidence and
        account-calendar changes require a full rescan, not a snapshot-id checkpoint.
        """
        self.conn.execute("SAVEPOINT first_play_refresh")
        try:
            imported = self.conn.execute(
                "SELECT value FROM settings WHERE key='takeout_imported_at'").fetchone()
            self._replace_first_play()
            self._first_play_settings(imported[0] if imported else None)
            self.conn.execute("RELEASE first_play_refresh")
        except BaseException:
            # SQLite may already have rolled back the transaction on interruption.
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK TO first_play_refresh")
                self.conn.execute("RELEASE first_play_refresh")
            raise

    @synchronized
    def first_play_map(self, kind) -> dict:
        from yt_playlist.core import listening_summaries
        if listening_summaries.available(self.conn):
            state = self.conn.execute('SELECT input_revision,built_revision FROM listening_summary_state WHERE id=1').fetchone()
            if not state or state[0]!=state[1]:
                return {}
        rows = self.conn.execute(
            "SELECT id_key, first_day FROM trend_first_play WHERE kind = ?", (kind,)).fetchall()
        return {r["id_key"]: r["first_day"] for r in rows}

    @synchronized
    def play_day_counts(self) -> list:
        """[(day, identity_key, count)] grouped by calendar date and recording.

        Count is distinct account listening days, matching listen_distribution.
        """
        rows = self.conn.execute(
            "SELECT CAST(day_ts/86400 AS INTEGER) day,identity_key k,COUNT(*) c "
            "FROM listening_days GROUP BY day,identity_key").fetchall()
        return [(r["day"], r["k"], r["c"]) for r in rows]

    @synchronized
    def track_meta(self) -> dict:
        """Current distinct canonical artist IDs and one representative genre per recording."""
        rows = self.conn.execute(
            "SELECT identity_key k, MIN(NULLIF(genre,'')) g "
            "FROM catalog_tracks GROUP BY identity_key").fetchall()
        credits = {}
        for key,aid in self.conn.execute('SELECT r.identity_key,c.artist_id FROM recording_listening_artists c '
                                         'JOIN recordings r ON r.id=c.recording_id ORDER BY c.artist_id'):
            credits.setdefault(key,[]).append('@artist:'+str(aid))
        return {r['k']:(tuple(credits.get(r['k'],())),r['g']) for r in rows}

    @synchronized
    def artist_labels(self):
        return {'@artist:'+str(r[0]):r[1] for r in self.conn.execute('SELECT id,name FROM artists')}

    @synchronized
    def day_artist_plays(self,since,until):
        return {r[0]:r[1] for r in self.conn.execute(
            "SELECT '@artist:'||a.artist_id,count(*) FROM listening_days d "
            'JOIN recording_listening_artists a USING(recording_id) '
            'WHERE d.day_ts>=? AND d.day_ts<? GROUP BY a.artist_id',(since,until))}

    @synchronized
    def track_cards(self, keys) -> dict:
        """{identity_key: {title, artist, thumbnail, album_browse_id}} batch lookup for insight art;
        empty dict for empty input."""
        keys = list(keys)
        if not keys:
            return {}
        ph = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key k, MIN(title) title, MIN(artist) artist, "
            f"MIN(NULLIF(thumbnail,'')) thumb, MIN(NULLIF(album_browse_id,'')) abid "
            f"FROM catalog_tracks WHERE identity_key IN ({ph}) GROUP BY identity_key", keys).fetchall()
        return {r["k"]: {"title": r["title"], "artist": r["artist"] or "",
                         "thumbnail": r["thumb"], "album_browse_id": r["abid"]} for r in rows}

    @synchronized
    def month_track_plays(self, since, until) -> dict:
        """FALLBACK ONLY (no play ledger). {identity_key: appearances} in [since, until).

        COUNT(*) here is snapshot appearances, NOT plays, and it has no day grouping (unlike
        play_day_counts right above). A track lingering in the recently-played window is counted once
        per sync, so this over-reports: on the reference database it turned 2 real plays into 14.
        Prefer ledger_track_plays(). trend_payload.month_review only reaches here when
        has_play_ledger() is False.
        """
        rows = self.conn.execute(
            "SELECT identity_key k,COUNT(*) c FROM listening_days "
            "WHERE day_ts>=? AND day_ts<? GROUP BY identity_key",
            (since, until)).fetchall()
        return {r["k"]: r["c"] for r in rows}
