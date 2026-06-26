"""ChartsRepo: play-history statistics for the charts / artist / playlist UI pages
(most-played songs and artists, per-playlist listen stats, and per-track detail views).
"""
from yt_playlist.repos.base import LIKED_EXISTS, Repo, synchronized

# Per-identity category expression for the non-playlist ticker dimensions. Each picks one
# representative value per identity_key (dup uploads of the same song collapse to one), and
# yields NULL for untagged songs so they drop out of the distribution rather than bucketing
# as ''. `year` floors the 4-digit mb_year to its decade ("1995" -> "1990").
_CAT_EXPR = {
    "genre": "MIN(NULLIF(genre,''))",
    "album": "MIN(NULLIF(album,''))",
    "artist": "MIN(NULLIF(artist,''))",
    # year: take the first 4 chars of mb_year (substr ...,1,4); GLOB '[0-9]x4' gates it to a real
    # 4-digit year (else CASE yields NULL -> dropped); CAST to INTEGER, //10*10 floors to the decade,
    # CAST back to TEXT so it shares the string category column with the other dimensions.
    "year": ("CASE WHEN substr(MIN(NULLIF(mb_year,'')),1,4) GLOB '[0-9][0-9][0-9][0-9]' "
             "THEN CAST(CAST(substr(MIN(NULLIF(mb_year,'')),1,4) AS INTEGER)/10*10 AS TEXT) "
             "END"),
}
# Distinct (song, playlist) membership: dedups dup track rows so a play counts once per playlist.
_PL_MEMBERSHIP = ("SELECT DISTINCT t.identity_key ik, pt.playlist_id pid "
                  "FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id")


class ChartsRepo(Repo):
    @synchronized
    def album_browse_ids(self) -> dict:
        """{album_title: a representative album_browse_id} for albums that have one, lets the
        Albums ticker link each row to its /album page. Titles without a browse id are omitted."""
        rows = self.conn.execute(
            "SELECT album, MIN(album_browse_id) b FROM tracks "
            "WHERE album<>'' AND album_browse_id IS NOT NULL AND album_browse_id<>'' "
            "GROUP BY album").fetchall()
        return {r["album"]: r["b"] for r in rows}

    @synchronized
    def history_bounds(self) -> tuple:
        """(earliest, latest) snapshot taken_at across all sync history, or (None, None) if empty.
        Lets the ticker size its candle periods to how much history actually exists."""
        r = self.conn.execute(
            "SELECT MIN(taken_at) lo, MAX(taken_at) hi FROM history_snapshots").fetchone()
        if r is None or r["lo"] is None:
            return (None, None)
        return (r["lo"], r["hi"])

    @synchronized
    def corpus_distribution(self, dimension) -> dict:
        """Library composition for a ticker dimension: {category: song_count}, counted per
        distinct identity_key (one song = one unit) so it's comparable with listen_distribution.

        dimension: 'genre' | 'year' | 'album' | 'playlist'. Untagged songs are excluded.
        Playlist counts a song once per playlist it belongs to (memberships sum > #songs).
        """
        if dimension == "playlist":
            rows = self.conn.execute(
                f"WITH pl AS ({_PL_MEMBERSHIP}) "
                "SELECT p.title cat, COUNT(DISTINCT pl.ik) c "
                "FROM pl JOIN playlists p ON p.id=pl.pid GROUP BY p.id").fetchall()
        else:
            rows = self.conn.execute(
                f"WITH ident AS (SELECT identity_key, {_CAT_EXPR[dimension]} cat "
                "FROM tracks GROUP BY identity_key) "
                "SELECT cat, COUNT(*) c FROM ident WHERE cat IS NOT NULL GROUP BY cat").fetchall()
        return {r["cat"]: r["c"] for r in rows}

    @synchronized
    def listen_distribution(self, dimension, since=None, until=None) -> dict:
        """Plays per category in a time window: {category: play_count}, a "play" = one history-item
        appearance whose snapshot taken_at is in [since, until) (None bound = open that side).
        Same dimensions/units as corpus_distribution, so shares of the two are directly comparable.
        Disjoint [since, until) periods give the ticker candle its open/close/high/low.
        """
        bounds = "(:since IS NULL OR hs.taken_at>=:since) AND (:until IS NULL OR hs.taken_at<:until)"
        args = {"since": since, "until": until}
        if dimension == "playlist":
            rows = self.conn.execute(
                f"WITH pl AS ({_PL_MEMBERSHIP}) "
                "SELECT p.title cat, COUNT(*) c "
                "FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
                "JOIN pl ON pl.ik=hi.identity_key JOIN playlists p ON p.id=pl.pid "
                f"WHERE {bounds} GROUP BY p.id", args).fetchall()
        else:
            rows = self.conn.execute(
                f"WITH ident AS (SELECT identity_key, {_CAT_EXPR[dimension]} cat "
                "FROM tracks GROUP BY identity_key) "
                "SELECT i.cat, COUNT(*) c "
                "FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
                "JOIN ident i ON i.identity_key=hi.identity_key "
                f"WHERE i.cat IS NOT NULL AND {bounds} "
                "GROUP BY i.cat", args).fetchall()
        return {r["cat"]: r["c"] for r in rows}

    @synchronized
    def get_playlist_listen_stats(self) -> dict:
        """Per-playlist {playlist_id: (last_listen_ts | None, listen_count)} from sync history.

        listen_count = times the playlist's tracks appear across history snapshots; last = newest
        snapshot containing any of them. Playlists with no recorded listens are absent.
        """
        rows = self.conn.execute(
            "SELECT pt.playlist_id AS pid, COUNT(hi.identity_key) AS cnt, MAX(hs.taken_at) AS last "
            "FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.track_id "
            "JOIN history_items hi ON hi.identity_key = t.identity_key "
            "JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "GROUP BY pt.playlist_id").fetchall()
        return {r["pid"]: (r["last"], r["cnt"]) for r in rows}

    @synchronized
    def get_playlist_track_recency(self) -> dict:
        """Per-playlist {playlist_id: [per-track last-played ts | None, ...]}, one entry per distinct
        track, its newest snapshot (None = never played). Unlike get_playlist_listen_stats (which
        collapses to the single freshest track), this keeps every track's recency so callers can judge
        a playlist by the *aggregate* staleness of its tracks, not its one most-recently-played song.
        """
        rows = self.conn.execute(
            "SELECT pt.playlist_id AS pid, MAX(hs.taken_at) AS last "
            "FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.track_id "
            "LEFT JOIN history_items hi ON hi.identity_key = t.identity_key "
            "LEFT JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "GROUP BY pt.playlist_id, pt.track_id").fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["pid"], []).append(r["last"])
        return out

    @synchronized
    def top_tracks(self, limit=100, since=None) -> list[dict]:
        """Most-played songs from sync history: play count = appearances across history snapshots.

        `since` (unix ts) limits to snapshots at/after that time, for a time-windowed chart.
        """
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key, COUNT(*) c FROM history_items hi "
            "  JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            "  WHERE (:since IS NULL OR hs.taken_at >= :since) GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(title) title, MIN(artist) artist, MIN(video_id) vid, "
            "               MIN(thumbnail) thumb FROM tracks GROUP BY identity_key) "
            "SELECT n.title, n.artist, n.vid, n.thumb, p.c FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "WHERE n.title <> '' ORDER BY p.c DESC, n.title LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"title": r["title"], "artist": r["artist"], "video_id": r["vid"],
                 "thumbnail": r["thumb"], "plays": r["c"]} for r in rows]

    @synchronized
    def top_artists(self, limit=100, since=None) -> list[dict]:
        """Most-played artists from sync history: play count summed over the artist's songs."""
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key, COUNT(*) c FROM history_items hi "
            "  JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            "  WHERE (:since IS NULL OR hs.taken_at >= :since) GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(artist) artist FROM tracks GROUP BY identity_key) "
            "SELECT n.artist, SUM(p.c) total, "
            "       (SELECT MIN(thumbnail) FROM tracks t2 WHERE t2.artist=n.artist AND t2.thumbnail IS NOT NULL) thumb "
            "FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "WHERE n.artist <> '' GROUP BY n.artist ORDER BY total DESC, n.artist LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"artist": r["artist"], "plays": r["total"], "thumbnail": r["thumb"]} for r in rows]

    @synchronized
    def playlist_tracks_detail(self, playlist_id) -> list[dict]:
        """Full per-track detail for our own playlist view (in playlist order)."""
        rows = self.conn.execute(
            "SELECT t.video_id vid, t.identity_key ikey, t.title, t.artist, t.orig_title otitle, t.orig_artist oartist, t.album, t.album_browse_id abrowse, "
            "       t.duration_s dur, t.available avail, t.thumbnail thumb, t.genre, t.mb_year, "
            "       (SELECT COUNT(*) FROM history_items hi WHERE hi.identity_key=t.identity_key) plays, "
            f"      {LIKED_EXISTS} liked "
            "FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"video_id": r["vid"], "identity_key": r["ikey"], "title": r["title"], "artist": r["artist"],
                 "title_edited": bool(r["otitle"] is not None and r["title"] != r["otitle"]),
                 "artist_edited": bool(r["oartist"] is not None and r["artist"] != r["oartist"]),
                 "album": r["album"] or "", "album_browse": r["abrowse"], "duration": r["dur"],
                 "available": r["avail"], "thumbnail": r["thumb"], "plays": r["plays"], "liked": bool(r["liked"]),
                 "genre": r["genre"] or "", "year": r["mb_year"] or ""} for r in rows]

    @synchronized
    def album_tracks_detail(self, album_browse_id) -> list[dict]:
        """Per-track detail for a saved album's folded-in tracks (same shape as playlist_tracks_detail,
        so the row partial renders identically). Album order ≈ insertion order (t.id)."""
        rows = self.conn.execute(
            "SELECT t.video_id vid, t.title, t.artist, t.album, t.album_browse_id abrowse, "
            "       t.duration_s dur, t.available avail, t.thumbnail thumb, t.genre, t.mb_year, "
            "       (SELECT COUNT(*) FROM history_items hi WHERE hi.identity_key=t.identity_key) plays, "
            f"      {LIKED_EXISTS} liked "
            "FROM tracks t WHERE t.album_browse_id=? ORDER BY t.id", (album_browse_id,)).fetchall()
        return [{"video_id": r["vid"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "album_browse": r["abrowse"], "duration": r["dur"], "available": r["avail"],
                 "thumbnail": r["thumb"], "plays": r["plays"], "liked": bool(r["liked"]),
                 "genre": r["genre"] or "", "year": r["mb_year"] or ""} for r in rows]

    @synchronized
    def artist_songs(self, artist) -> list[dict]:
        """An artist's songs that appear in your playlists: play count + which playlists hold each."""
        songs = self.conn.execute(
            "SELECT t.identity_key key, MIN(t.title) title, MIN(t.album) album, MIN(t.video_id) vid, "
            "       MIN(t.duration_s) dur, MIN(t.thumbnail) thumb, MIN(t.album_browse_id) abrowse, "
            "       (SELECT COUNT(*) FROM history_items hi WHERE hi.identity_key=t.identity_key) plays, "
            f"      {LIKED_EXISTS} liked "
            "FROM tracks t WHERE t.artist=? GROUP BY t.identity_key", (artist,)).fetchall()
        membership = self.conn.execute(
            "SELECT DISTINCT t.identity_key key, pl.title title, pl.ytm_playlist_id ytm FROM tracks t "
            "JOIN playlist_tracks pt ON pt.track_id=t.id JOIN playlists pl ON pl.id=pt.playlist_id "
            "WHERE t.artist=?", (artist,)).fetchall()
        by_key = {}
        for r in membership:
            by_key.setdefault(r["key"], []).append({"title": r["title"], "ytm": r["ytm"]})
        out = [{"title": r["title"], "album": r["album"] or "", "video_id": r["vid"],
                "duration": r["dur"], "plays": r["plays"], "thumbnail": r["thumb"],
                "album_browse": r["abrowse"], "liked": bool(r["liked"]),
                "playlists": sorted(by_key.get(r["key"], []), key=lambda p: p["title"].lower())}
               for r in songs]
        out.sort(key=lambda s: (-s["plays"], (s["title"] or "").lower()))
        return out
