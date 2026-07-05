"""TrendsRepo: read/aggregation queries behind the Trends page + its precomputed rollup. Owns the
first-play index (trend_first_play) and the day-model time-series and health queries. Aggregation
happens in rec/trend_rollups.py; this repo only fetches raw material and upserts the index."""
from yt_playlist.repos.base import Repo, synchronized, GENERATED_GROUP


class TrendsRepo(Repo):
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
        """{identity_key: plays} per-track day-model plays in [since, until); feeds Month-in-review's
        song-of-the-month (listen_distribution has no track dimension)."""
        rows = self.conn.execute(
            "SELECT hi.identity_key k, COUNT(*) c FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "WHERE hs.taken_at >= ? AND hs.taken_at < ? GROUP BY hi.identity_key",
            (since, until)).fetchall()
        return {r["k"]: r["c"] for r in rows}
