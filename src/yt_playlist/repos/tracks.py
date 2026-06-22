"""TrackRepo — track rows and their genre/year enrichment."""
from yt_playlist.util.matching import identity_key
from yt_playlist.repos.base import Repo, synchronized


class TrackRepo(Repo):
    @synchronized
    def upsert_track(self, video_id, title, artist, album, duration_s, available=None,
                     video_type=None, artist_browse_id=None, album_browse_id=None, thumbnail=None) -> int:
        key = identity_key(title, artist)
        row = self.conn.execute(
            "SELECT id FROM tracks WHERE identity_key=? AND IFNULL(video_id,'')=IFNULL(?,'')",
            (key, video_id)).fetchone()
        if row:
            # keep these fresh on re-sync (backfills existing rows once the data is available)
            for col, val in (("available", None if available is None else int(available)),
                             ("video_type", video_type),
                             ("artist_browse_id", artist_browse_id),
                             ("album_browse_id", album_browse_id),
                             ("thumbnail", thumbnail)):
                if val is not None:
                    self.conn.execute(f"UPDATE tracks SET {col}=? WHERE id=?", (val, row["id"]))
            self.conn.commit()
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO tracks(video_id,title,artist,album,duration_s,identity_key,available,"
            "video_type,artist_browse_id,album_browse_id,thumbnail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (video_id, title, artist, album, duration_s, key,
             None if available is None else int(available), video_type,
             artist_browse_id, album_browse_id, thumbnail))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def tracks_to_enrich(self, playlist_id) -> list:
        """Tracks in this playlist still missing genre or year (NULL or blank), in playlist order.
        Blank counts as missing so re-running enrichment retries tracks that didn't fully resolve."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? "
            "AND (t.genre IS NULL OR t.genre = '' OR t.mb_year IS NULL OR t.mb_year = '') "
            "ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "artist": r["artist"]}
                for r in rows]

    @synchronized
    def album_tracks_to_enrich(self, album_browse_id) -> list:
        """A saved album's folded-in tracks still missing genre or year — the album-scoped twin of
        tracks_to_enrich, so the same enrich runners work over an album."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist FROM tracks t WHERE t.album_browse_id=? "
            "AND (t.genre IS NULL OR t.genre = '' OR t.mb_year IS NULL OR t.mb_year = '') "
            "ORDER BY t.id", (album_browse_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "artist": r["artist"]}
                for r in rows]

    @synchronized
    def set_track_genre(self, track_id, genre) -> None:
        # manual override: set exactly what the user chose (may be blank to clear)
        self.conn.execute("UPDATE tracks SET genre=? WHERE id=?", (genre or "", track_id))
        self.conn.commit()

    @synchronized
    def set_track_year(self, track_id, year) -> None:
        # manual override: set exactly what the user typed (may be blank to clear)
        self.conn.execute("UPDATE tracks SET mb_year=? WHERE id=?", (year or "", track_id))
        self.conn.commit()

    @synchronized
    def tracks_missing_genre(self, playlist_id) -> list:
        """Playlist tracks with no genre yet (for Last.fm genre enrichment), in playlist order."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? "
            "AND (t.genre IS NULL OR t.genre = '') ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "artist": r["artist"]}
                for r in rows]

    @synchronized
    def set_track_enrichment(self, track_id, genre, year) -> None:
        # fill-only: set a field just when it's currently blank. So enrichment fills gaps and never
        # overwrites what you already have — MusicBrainz and Last.fm each top up the other's misses.
        self.conn.execute(
            "UPDATE tracks SET "
            "  genre = CASE WHEN (genre IS NULL OR genre='') AND ? <> '' THEN ? ELSE genre END, "
            "  mb_year = CASE WHEN (mb_year IS NULL OR mb_year='') AND ? <> '' THEN ? ELSE mb_year END "
            "WHERE id=?",
            (genre or "", genre or "", year or "", year or "", track_id))
        self.conn.commit()

    @synchronized
    def get_track_enrichment(self, track_id):
        """Current (genre, year) for a track — used to report the effective value after a fill."""
        row = self.conn.execute("SELECT genre, mb_year FROM tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return ("", "")
        return (row["genre"] or "", row["mb_year"] or "")

    @synchronized
    def track_ids_for_videos(self, video_ids) -> dict:
        """Map video_id -> track_id for tracks already in the store (latest row wins)."""
        out = {}
        for vid in video_ids:
            row = self.conn.execute(
                "SELECT id FROM tracks WHERE video_id=? ORDER BY id DESC LIMIT 1", (vid,)).fetchone()
            if row is not None:
                out[vid] = row["id"]
        return out

    @synchronized
    def materialized_album_ids(self) -> set:
        """browse_ids of saved albums whose tracks we've already folded into the library — so sync
        only fetches an album's track list once, not on every pass."""
        return {r["album_browse_id"] for r in self.conn.execute(
            "SELECT DISTINCT album_browse_id FROM tracks WHERE album_browse_id IS NOT NULL")}
