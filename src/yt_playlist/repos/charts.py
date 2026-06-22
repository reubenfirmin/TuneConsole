"""ChartsRepo — play-history statistics for the charts / artist / playlist UI pages
(most-played songs and artists, per-playlist listen stats, and per-track detail views).
"""
from yt_playlist.repos.base import LIKED_EXISTS, Repo, synchronized


class ChartsRepo(Repo):
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
    def top_tracks(self, limit=100, since=None) -> list[dict]:
        """Most-played songs from sync history — play count = appearances across history snapshots.

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
        """Most-played artists from sync history — play count summed over the artist's songs."""
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
            "SELECT t.video_id vid, t.identity_key ikey, t.title, t.artist, t.album, t.album_browse_id abrowse, "
            "       t.duration_s dur, t.available avail, t.thumbnail thumb, t.genre, t.mb_year, "
            "       (SELECT COUNT(*) FROM history_items hi WHERE hi.identity_key=t.identity_key) plays, "
            f"      {LIKED_EXISTS} liked "
            "FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"video_id": r["vid"], "identity_key": r["ikey"], "title": r["title"], "artist": r["artist"],
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
