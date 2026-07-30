"""TrendsRepo: read/aggregation queries behind the Trends page + its precomputed rollup. Owns the
first-play index (trend_first_play) and the day-model time-series and health queries. Aggregation
happens in rec/trend_rollups.py; this repo only fetches raw material and upserts the index."""
from yt_playlist.repos.base import Repo, synchronized, GENERATED_GROUP


class TrendsRepo(Repo):
    # -- the play ledger -----------------------------------------------------------------------
    # play_events is one row per REAL play, with a real timestamp and (for live-captured rows) the
    # playlist it was played from. It is the only source that can honour the GENERATED_GROUP
    # quarantine (see repos/base.py), because the history day model carries no provenance at all.
    #
    # Everything below returns the same shapes the history queries return, so the rollup consumes
    # either. The difference is what a count MEANS: a real play here, a snapshot appearance there.

    def _generated_filter(self, exclude_generated):
        """(sql_fragment, args) restricting play_events (aliased `pe`) to non-generated plays.

        Un-decorated so the @synchronized public methods can reuse it. A NULL playlist_ytm_id (every
        Takeout-backfilled row) cannot be proven generated, so it is kept.
        """
        if not exclude_generated:
            return "", []
        rows = self.conn.execute(
            "SELECT ytm FROM playlist_group WHERE name = ?", (GENERATED_GROUP,)).fetchall()
        gen = sorted(r["ytm"] for r in rows)
        if not gen:
            return "", []
        ph = ",".join("?" * len(gen))
        return f" AND (pe.playlist_ytm_id IS NULL OR pe.playlist_ytm_id NOT IN ({ph}))", gen

    @synchronized
    def generated_ytm_ids(self) -> set[str]:
        """The ytm ids of playlists this app generated. Plays sourced from these are the app's own
        recommendations echoed back, not listening the user chose."""
        rows = self.conn.execute(
            "SELECT ytm FROM playlist_group WHERE name = ?", (GENERATED_GROUP,)).fetchall()
        return {r["ytm"] for r in rows}

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
            "SELECT DISTINCT t.artist a FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.track_id "
            "JOIN playlists p ON p.id = pt.playlist_id "
            "WHERE t.artist <> '' AND p.ytm_playlist_id NOT IN "
            "  (SELECT ytm FROM playlist_group WHERE name = ?)", (GENERATED_GROUP,)).fetchall()
        return {r["a"] for r in rows}

    @synchronized
    def catalog_track_keys(self) -> set[str]:
        """identity_keys in a playlist the user owns. The track-level twin of catalog_artists()."""
        rows = self.conn.execute(
            "SELECT DISTINCT t.identity_key k FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.track_id "
            "JOIN playlists p ON p.id = pt.playlist_id "
            "WHERE p.ytm_playlist_id NOT IN "
            "  (SELECT ytm FROM playlist_group WHERE name = ?)", (GENERATED_GROUP,)).fetchall()
        return {r["k"] for r in rows}

    @synchronized
    def has_play_ledger(self) -> bool:
        """True when play_events holds anything. False for a fresh install with no extension and no
        Takeout import, where the coarse history day model is still the only evidence of listening."""
        return self.conn.execute("SELECT 1 FROM play_events LIMIT 1").fetchone() is not None

    @synchronized
    def ledger_day_counts(self, exclude_generated=True) -> list:
        """[(day, identity_key, plays)] from the play ledger: one row per (UTC day, track), plays =
        the number of REAL plays that day. Same shape as play_day_counts(), so the rollup consumes it
        unchanged, but the number means plays rather than snapshot appearances.

        Identities are merged, not distinguished. The history day model keyed its dedupe on
        (identity, date), which double-counted a track played on two accounts the same day.
        """
        frag, args = self._generated_filter(exclude_generated)
        where = f"WHERE 1=1{frag}" if frag else ""
        rows = self.conn.execute(
            f"SELECT CAST(pe.played_at / 86400 AS INTEGER) day, pe.identity_key k, COUNT(*) c "
            f"FROM play_events pe {where} GROUP BY day, k", args).fetchall()
        return [(r["day"], r["k"], r["c"]) for r in rows]

    @synchronized
    def ledger_track_plays(self, since, until, exclude_generated=True) -> dict:
        """{identity_key: plays} real plays in [since, until). Replaces month_track_plays, whose
        COUNT(*) over history_items counted a lingering snapshot window as repeat plays."""
        frag, args = self._generated_filter(exclude_generated)
        rows = self.conn.execute(
            f"SELECT pe.identity_key k, COUNT(*) c FROM play_events pe "
            f"WHERE pe.played_at >= ? AND pe.played_at < ?{frag} GROUP BY pe.identity_key",
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

    @synchronized
    def play_hours(self, since, until, exclude_generated=True) -> list:
        """24-bucket hour-of-day histogram (local? no -- UTC) of real plays in [since, until). Feeds the
        recap personality's rhythm axis."""
        frag, args = self._generated_filter(exclude_generated)
        hours = [0] * 24
        for r in self.conn.execute(
                f"SELECT CAST(strftime('%H', pe.played_at, 'unixepoch') AS INTEGER) h, COUNT(*) c "
                f"FROM play_events pe WHERE pe.played_at >= ? AND pe.played_at < ?{frag} GROUP BY h",
                [since, until, *args]).fetchall():
            if r["h"] is not None:
                hours[r["h"]] = r["c"]
        return hours

    @synchronized
    def ledger_artist_plays(self, since, until, exclude_generated=True) -> dict:
        """{artist: plays} real plays in [since, until), artist resolved through tracks. Tracks with a
        blank or missing artist are skipped. Replaces charts.listen_distribution('artist', ...) inside
        the Trends rollup only; the charts/ticker consumers keep their own day-model semantics."""
        frag, args = self._generated_filter(exclude_generated)
        rows = self.conn.execute(
            f"SELECT a artist, COUNT(*) c FROM ("
            f"  SELECT (SELECT MIN(t.artist) FROM tracks t WHERE t.identity_key = pe.identity_key) a "
            f"  FROM play_events pe WHERE pe.played_at >= ? AND pe.played_at < ?{frag}"
            f") WHERE a IS NOT NULL AND a <> '' GROUP BY a",
            [since, until, *args]).fetchall()
        return {r["artist"]: r["c"] for r in rows}

    # -- the history day model (fallback; no provenance) ---------------------------------------
    @synchronized
    def max_snapshot_id(self) -> int:
        r = self.conn.execute("SELECT MAX(id) m FROM history_snapshots").fetchone()
        return int(r["m"]) if r and r["m"] is not None else 0

    @synchronized
    def history_track_first(self, after_id) -> dict:
        """{identity_key: (first_day, first_ts)} earliest snapshot appearance among snapshots with
        id > after_id (the incremental window). day = int(taken_at // 86400)."""
        rows = self.conn.execute(
            "SELECT hi.identity_key k, MIN(hs.taken_at) ts "
            "FROM history_items hi JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "WHERE hs.id > ? GROUP BY hi.identity_key", (int(after_id),)).fetchall()
        return {r["k"]: (int(r["ts"] // 86400), r["ts"]) for r in rows}

    @synchronized
    def play_event_track_first(self) -> dict:
        """{identity_key: (first_day, first_ts)} earliest real play from play_events (indexed on time)."""
        rows = self.conn.execute(
            "SELECT identity_key k, MIN(played_at) ts FROM play_events GROUP BY identity_key").fetchall()
        return {r["k"]: (int(r["ts"] // 86400), r["ts"]) for r in rows}

    @synchronized
    def upsert_first_play_min(self, rows) -> None:
        """rows = [(kind, id_key, first_day, first_ts, source)]; keeps the LOWER first_ts on conflict
        (so a Takeout backfill or a play_event can only pull first-seen earlier, never later)."""
        self.conn.executemany(
            "INSERT INTO trend_first_play(kind, id_key, first_day, first_ts, source) VALUES (?,?,?,?,?) "
            "ON CONFLICT(kind, id_key) DO UPDATE SET "
            "  first_day = CASE WHEN excluded.first_ts < trend_first_play.first_ts "
            "                   THEN excluded.first_day ELSE trend_first_play.first_day END, "
            "  first_ts  = MIN(trend_first_play.first_ts, excluded.first_ts), "
            "  source    = CASE WHEN excluded.first_ts < trend_first_play.first_ts "
            "                   THEN excluded.source ELSE trend_first_play.source END",
            list(rows))
        self.conn.commit()

    @synchronized
    def rebuild_artist_first_play(self) -> None:
        """Derive kind='artist' rows from the current kind='track' rows: an artist's first day is the
        MIN over its tracks. source = the model that gave the winning (lowest-ts) track."""
        self.conn.execute("DELETE FROM trend_first_play WHERE kind = 'artist'")
        self.conn.execute(
            "INSERT INTO trend_first_play(kind, id_key, first_day, first_ts, source) "
            "SELECT 'artist', t.artist, MIN(fp.first_day), MIN(fp.first_ts), "
            "  (SELECT fp2.source FROM trend_first_play fp2 JOIN tracks t2 ON t2.identity_key = fp2.id_key "
            "   WHERE fp2.kind = 'track' AND t2.artist = t.artist ORDER BY fp2.first_ts LIMIT 1) "
            "FROM trend_first_play fp JOIN tracks t ON t.identity_key = fp.id_key "
            "WHERE fp.kind = 'track' AND t.artist <> '' GROUP BY t.artist")
        self.conn.commit()

    @synchronized
    def clear_first_play(self) -> None:
        self.conn.execute("DELETE FROM trend_first_play")
        self.conn.commit()

    @synchronized
    def first_play_map(self, kind) -> dict:
        rows = self.conn.execute(
            "SELECT id_key, first_day FROM trend_first_play WHERE kind = ?", (kind,)).fetchall()
        return {r["id_key"]: r["first_day"] for r in rows}

    @synchronized
    def first_play_floor_day(self):
        r = self.conn.execute(
            "SELECT MIN(first_day) d FROM trend_first_play WHERE kind = 'track'").fetchone()
        return r["d"] if r and r["d"] is not None else None

    @synchronized
    def play_day_counts(self) -> list:
        """[(day, identity_key, count)] one row per (UTC day, key), count = history-item appearances
        that day. Day-model semantics, matching listen_distribution."""
        rows = self.conn.execute(
            "SELECT CAST(hs.taken_at / 86400 AS INTEGER) day, hi.identity_key k, COUNT(*) c "
            "FROM history_items hi JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "GROUP BY day, hi.identity_key").fetchall()
        return [(r["day"], r["k"], r["c"]) for r in rows]

    @synchronized
    def track_meta(self) -> dict:
        """{identity_key: (artist, genre_or_None)} with one representative genre per key
        (MIN(NULLIF(genre,'')), the charts _CAT_EXPR rule); genre None when untagged."""
        rows = self.conn.execute(
            "SELECT identity_key k, MIN(artist) a, MIN(NULLIF(genre,'')) g "
            "FROM tracks GROUP BY identity_key").fetchall()
        return {r["k"]: (r["a"] or "", r["g"]) for r in rows}

    @synchronized
    def never_played(self) -> tuple:
        """(total_tracks, never_played), per distinct identity_key."""
        row = self.conn.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN plays = 0 THEN 1 ELSE 0 END) never FROM ("
            "  SELECT t.identity_key, "
            "    (SELECT COUNT(*) FROM history_items hi WHERE hi.identity_key = t.identity_key) plays "
            "  FROM tracks t GROUP BY t.identity_key)").fetchone()
        return (row["total"] or 0, row["never"] or 0)

    @synchronized
    def track_last_play(self) -> list:
        """[(identity_key, last_ts_or_None)] newest snapshot per distinct track (None = never played)."""
        rows = self.conn.execute(
            "SELECT t.identity_key k, MAX(hs.taken_at) last FROM tracks t "
            "LEFT JOIN history_items hi ON hi.identity_key = t.identity_key "
            "LEFT JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "GROUP BY t.identity_key").fetchall()
        return [(r["k"], r["last"]) for r in rows]

    @synchronized
    def dead_playlists(self, max_listens=0) -> list:
        """[{playlist_id, title, last_listen, listens}] for playlists whose tracks were listened to at
        most max_listens times across history (LEFT JOIN keeps never-listened playlists), listens asc."""
        rows = self.conn.execute(
            "SELECT p.id pid, p.title title, "
            "  (SELECT MAX(hs.taken_at) FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
            "     JOIN history_items hi ON hi.identity_key = t.identity_key "
            "     JOIN history_snapshots hs ON hs.id = hi.snapshot_id WHERE pt.playlist_id = p.id) last, "
            "  (SELECT COUNT(hi.identity_key) FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
            "     JOIN history_items hi ON hi.identity_key = t.identity_key WHERE pt.playlist_id = p.id) cnt "
            "FROM playlists p "
            "WHERE p.id NOT IN (SELECT p2.id FROM playlists p2 "
            "  JOIN playlist_group g ON g.ytm = p2.ytm_playlist_id WHERE g.name = :grp)",
            {"grp": GENERATED_GROUP}).fetchall()
        out = [{"playlist_id": r["pid"], "title": r["title"], "last_listen": r["last"],
                "listens": r["cnt"] or 0} for r in rows]
        out = [d for d in out if d["listens"] <= max_listens]
        out.sort(key=lambda d: (d["listens"], (d["title"] or "").lower()))
        return out

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
            f"FROM tracks WHERE identity_key IN ({ph}) GROUP BY identity_key", keys).fetchall()
        return {r["k"]: {"title": r["title"], "artist": r["artist"] or "",
                         "thumbnail": r["thumb"], "album_browse_id": r["abid"]} for r in rows}

    @synchronized
    def rediscover_tracks(self, before_ts, limit=3) -> list:
        """[{identity_key, title, artist, thumbnail, plays, last_play}] owned tracks with the most
        lifetime day-model plays whose newest play is < before_ts, plays desc."""
        rows = self.conn.execute(
            "SELECT t.identity_key k, MIN(t.title) title, MIN(t.artist) artist, "
            "  MIN(NULLIF(t.thumbnail,'')) thumb, COUNT(hi.identity_key) plays, MAX(hs.taken_at) last "
            "FROM tracks t JOIN history_items hi ON hi.identity_key = t.identity_key "
            "JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "GROUP BY t.identity_key HAVING plays > 0 AND last < ? "
            "ORDER BY plays DESC, last ASC LIMIT ?", (before_ts, int(limit))).fetchall()
        return [{"identity_key": r["k"], "title": r["title"], "artist": r["artist"] or "",
                 "thumbnail": r["thumb"], "plays": r["plays"], "last_play": r["last"]} for r in rows]

    @synchronized
    def month_track_plays(self, since, until) -> dict:
        """FALLBACK ONLY (no play ledger). {identity_key: appearances} in [since, until).

        COUNT(*) here is snapshot appearances, NOT plays, and it has no day grouping (unlike
        play_day_counts right above). A track lingering in the recently-played window is counted once
        per sync, so this over-reports: on the reference database it turned 2 real plays into 14.
        Prefer ledger_track_plays(). trend_rollups.month_review only reaches here when
        has_play_ledger() is False.
        """
        rows = self.conn.execute(
            "SELECT hi.identity_key k, COUNT(*) c FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "WHERE hs.taken_at >= ? AND hs.taken_at < ? GROUP BY hi.identity_key",
            (since, until)).fetchall()
        return {r["k"]: r["c"] for r in rows}
