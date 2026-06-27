"""TrackRepo: track rows and their genre/year enrichment."""
from yt_playlist.util.matching import identity_key
from yt_playlist.repos.base import Repo, synchronized


class TrackRepo(Repo):
    @synchronized
    def upsert_track(self, video_id, title, artist, album, duration_s, available=None,
                     video_type=None, artist_browse_id=None, album_browse_id=None, thumbnail=None,
                     created_at=None) -> int:
        key = identity_key(title, artist)
        row = self.conn.execute(
            "SELECT id FROM tracks WHERE identity_key=? AND IFNULL(video_id,'')=IFNULL(?,'')",
            (key, video_id)).fetchone()
        if row:
            # keep these fresh on re-sync (backfills existing rows once the data is available)
            for col, val in (("duration_s", duration_s),
                             ("available", None if available is None else int(available)),
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
            "video_type,artist_browse_id,album_browse_id,thumbnail,orig_title,orig_artist,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?, strftime('%s','now')))",
            (video_id, title, artist, album, duration_s, key,
             None if available is None else int(available), video_type,
             artist_browse_id, album_browse_id, thumbnail, title, artist, created_at))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def known_duration(self, title, artist):
        """A duration (seconds) we already hold for this song under ANY stored row, else None. Lets a
        generated playlist reuse a time we know from one videoId for the same song under another."""
        row = self.conn.execute(
            "SELECT MAX(duration_s) d FROM tracks WHERE identity_key=?",
            (identity_key(title, artist),)).fetchone()
        return row["d"] if row and row["d"] is not None else None

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
        """A saved album's folded-in tracks still missing genre or year, the album-scoped twin of
        tracks_to_enrich, so the same enrich runners work over an album."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist FROM tracks t WHERE t.album_browse_id=? "
            "AND (t.genre IS NULL OR t.genre = '' OR t.mb_year IS NULL OR t.mb_year = '') "
            "ORDER BY t.id", (album_browse_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "artist": r["artist"]}
                for r in rows]

    _NEEDS = ("t.genre IS NULL OR t.genre='' OR t.mb_year IS NULL OR t.mb_year='' "
              "OR t.bpm IS NULL OR t.energy IS NULL OR t.danceability IS NULL")

    @synchronized
    def tracks_for_waterfall(self, playlist_id) -> list:
        """Playlist tracks the waterfall still has work for (missing genre, year, or any of the
        core audio features) in playlist order. Carries mb_recording_id so AcousticBrainz can key
        off an already-resolved MBID."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id FROM playlist_tracks pt "
            f"JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? AND ({self._NEEDS}) "
            "ORDER BY pt.position", (playlist_id,)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def album_tracks_for_waterfall(self, album_browse_id) -> list:
        """A saved album's folded-in tracks the waterfall still has work for, album-scoped twin of
        tracks_for_waterfall."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id FROM tracks t "
            f"WHERE t.album_browse_id=? AND ({self._NEEDS}) ORDER BY t.id", (album_browse_id,)).fetchall()
        return [dict(r) for r in rows]

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
    def set_track_title(self, track_id, title) -> None:
        # manual fix: overwrite the live title in place so downstream consumers use it. Never blank.
        title = (title or "").strip()
        if not title:
            return
        self.conn.execute("UPDATE tracks SET title=? WHERE id=?", (title, track_id))
        self.conn.commit()

    @synchronized
    def set_track_artist(self, track_id, artist) -> None:
        # manual fix: overwrite the live artist in place. Never blank.
        artist = (artist or "").strip()
        if not artist:
            return
        self.conn.execute("UPDATE tracks SET artist=? WHERE id=?", (artist, track_id))
        self.conn.commit()

    @synchronized
    def reset_track_title(self, track_id) -> None:
        # restore the original YouTube title (undo a manual fix).
        self.conn.execute("UPDATE tracks SET title=orig_title WHERE id=? AND orig_title IS NOT NULL",
                          (track_id,))
        self.conn.commit()

    @synchronized
    def reset_track_artist(self, track_id) -> None:
        self.conn.execute("UPDATE tracks SET artist=orig_artist WHERE id=? AND orig_artist IS NOT NULL",
                          (track_id,))
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
        # overwrites what you already have. MusicBrainz and Last.fm each top up the other's misses.
        self.conn.execute(
            "UPDATE tracks SET "
            "  genre = CASE WHEN (genre IS NULL OR genre='') AND ? <> '' THEN ? ELSE genre END, "
            "  mb_year = CASE WHEN (mb_year IS NULL OR mb_year='') AND ? <> '' THEN ? ELSE mb_year END "
            "WHERE id=?",
            (genre or "", genre or "", year or "", year or "", track_id))
        self.conn.commit()

    @synchronized
    def get_track_enrichment(self, track_id):
        """Current (genre, year) for a track, used to report the effective value after a fill."""
        row = self.conn.execute("SELECT genre, mb_year FROM tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return ("", "")
        return (row["genre"] or "", row["mb_year"] or "")

    @synchronized
    def set_track_audio(self, track_id, bpm=None, energy=None, danceability=None,
                        music_key=None, music_scale=None, mood_happy=None, mood_sad=None,
                        mood_relaxed=None, mood_acoustic=None, instrumental=None,
                        loudness=None, dynamic_complexity=None,
                        popularity=None, gain=None, label=None) -> None:
        # fill-only: each provided (non-None) field fills its column only when it's currently NULL,
        # so re-running a provider (or a second provider) tops up gaps without clobbering data.
        fields = {"bpm": bpm, "energy": energy, "danceability": danceability,
                  "music_key": music_key, "music_scale": music_scale,
                  "mood_happy": mood_happy, "mood_sad": mood_sad, "mood_relaxed": mood_relaxed,
                  "mood_acoustic": mood_acoustic, "instrumental": instrumental,
                  "loudness": loudness, "dynamic_complexity": dynamic_complexity,
                  "popularity": popularity, "gain": gain, "label": label}
        updates = {c: v for c, v in fields.items() if v is not None}
        if not updates:
            return
        # column names come from the fixed literal dict above (not user input) -> safe to inline
        sets = ", ".join(f"{c} = CASE WHEN {c} IS NULL THEN ? ELSE {c} END" for c in updates)
        self.conn.execute(f"UPDATE tracks SET {sets} WHERE id=?",
                          list(updates.values()) + [track_id])
        self.conn.commit()

    @synchronized
    def get_track_audio(self, track_id):
        """Current (bpm, energy, danceability) for a track, each float or None."""
        row = self.conn.execute(
            "SELECT bpm, energy, danceability FROM tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return (None, None, None)
        return (row["bpm"], row["energy"], row["danceability"])

    @synchronized
    def set_track_mbid(self, track_id, mbid) -> None:
        # fill-only: keep the first MBID we resolve for a track.
        if not mbid:
            return
        self.conn.execute(
            "UPDATE tracks SET mb_recording_id = CASE "
            "WHEN mb_recording_id IS NULL OR mb_recording_id='' THEN ? ELSE mb_recording_id END "
            "WHERE id=?", (mbid, track_id))
        self.conn.commit()

    @synchronized
    def tracks_missing_audio(self, playlist_id) -> list[dict]:
        """Playlist tracks still missing any audio feature (bpm/energy/danceability), in order.
        Carries mb_recording_id so AcousticBrainz can key off it without a second query."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? "
            "AND (t.bpm IS NULL OR t.energy IS NULL OR t.danceability IS NULL) "
            "ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"],
                 "artist": r["artist"], "mb_recording_id": r["mb_recording_id"]} for r in rows]

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
        """browse_ids of saved albums whose tracks we've already folded into the library, so sync
        only fetches an album's track list once, not on every pass."""
        return {r["album_browse_id"] for r in self.conn.execute(
            "SELECT DISTINCT album_browse_id FROM tracks WHERE album_browse_id IS NOT NULL")}
