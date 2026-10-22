"""TrackRepo: track rows and their genre/year enrichment."""
from yt_playlist.util.matching import identity_key, normalize, recording_title
from yt_playlist.core import catalog
from yt_playlist.repos.base import Repo, synchronized


class TrackRepo(Repo):
    @synchronized
    def associate_youtube_item(self, video_id, recording_id, *, source, evidence, now):
        with self.conn:
            catalog.associate_item(self.conn,video_id,recording_id,source=source,evidence=evidence,now=now)
    @synchronized
    def resolve_identity_key(self, key, video_id=None):
        return catalog.resolve_key(self.conn, key, video_id)

    @synchronized
    def match_recording(self, title, artist):
        """Resolve text-only imports conservatively, retaining performance/version qualifiers."""
        rows = self.conn.execute(
            'SELECT DISTINCT c.recording_id,c.identity_key,c.title,c.artist FROM catalog_tracks c '
            'JOIN recording_aliases a ON a.recording_id=c.recording_id WHERE a.alias=?',
            (identity_key(title,artist),)).fetchall()
        keys = {r['identity_key'] for r in rows if recording_title(r['title']) == recording_title(title)
                and normalize(r['artist']) == normalize(artist)}
        return next(iter(keys)) if len(keys)==1 else None

    @synchronized
    def catalog_subject(self, track_id):
        return self.track_for_waterfall(track_id)

    @synchronized
    def observe_track(self, video_id, title, artist, *, now=None, album=None, album_browse_id=None, credits=None):
        entry_id = self.observe_track_entry(video_id,title,artist,now=now,album=album,
                                           album_browse_id=album_browse_id,credits=credits)
        return self.conn.execute('SELECT identity_key FROM catalog_tracks WHERE id=?', (entry_id,)).fetchone()[0]

    @synchronized
    def observe_track_entry(self, video_id, title, artist, *, now=None, album=None, album_browse_id=None, credits=None):
        """Retain an exact source entry without acquiring library membership."""
        with self.conn:
            return catalog.upsert(self.conn,video_id,title,artist,album=album,
                                      album_browse_id=album_browse_id,created_at=now,library=False,credits=credits)

    @synchronized
    def record_item_rating(self, identity_id, video_id, status, *, source, now):
        self.conn.execute('INSERT INTO item_ratings VALUES (?,?,?,?,?) ON CONFLICT(identity_id,video_id) DO UPDATE SET status=excluded.status,source=excluded.source,observed_at=excluded.observed_at', (identity_id,video_id,status,source,now))
        self.conn.commit()

    @synchronized
    def item_rating(self, identity_id, video_id):
        row = self.conn.execute(
            "SELECT status,source,observed_at FROM item_ratings WHERE identity_id=? AND video_id=?",
            (identity_id, video_id)).fetchone()
        return dict(row) if row else None

    @synchronized
    def pending_action_ratings(self, identity_id):
        """Live ratings not yet reflected in the model, including unresolved exact uploads."""
        return [dict(row) for row in self.conn.execute(
            "SELECT ir.video_id,ir.status,ir.observed_at FROM item_ratings ir "
            "LEFT JOIN catalog_tracks c ON c.video_id=ir.video_id "
            "WHERE ir.identity_id=? AND ir.source IN ('api','action') AND ("
            "(ir.status IN ('LIKE','DISLIKE') AND NOT EXISTS (SELECT 1 FROM rec_feedback f "
            "WHERE f.item_key=c.identity_key AND f.kind=lower(ir.status))) OR "
            "(ir.status='INDIFFERENT' AND EXISTS (SELECT 1 FROM rec_feedback f "
            "WHERE f.item_key=c.identity_key AND f.kind IN ('like','dislike')))) "
            "ORDER BY ir.observed_at,ir.video_id",
            (identity_id,))]

    @synchronized
    def upsert_track(self, video_id, title, artist, album, duration_s, available=None,
                     video_type=None, artist_browse_id=None, album_browse_id=None, thumbnail=None,
                     created_at=None, *, source_ref=None, credits=None) -> int:
        with self.conn:
            return catalog.upsert(
                self.conn, video_id, title, artist, album, duration_s, available,
                video_type, artist_browse_id, album_browse_id, thumbnail, created_at,
                library=True, source_ref=source_ref,credits=credits)

    @synchronized
    def identity_key_for_video(self, video_id) -> str | None:
        """Resolve an observed upload in the catalog, independently of library membership."""
        row = self.conn.execute(
            "SELECT identity_key FROM catalog_tracks WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
        return row["identity_key"] if row else None

    @synchronized
    def known_duration(self, title, artist):
        """A duration (seconds) we already hold for this song under ANY stored row, else None. Lets a
        generated playlist reuse a time we know from one videoId for the same song under another."""
        rows = self.conn.execute(
            "SELECT DISTINCT recording_id,duration_s,title,artist FROM catalog_tracks "
            "WHERE identity_key=?", (catalog.resolve_key(self.conn,identity_key(title,artist)),)).fetchall()
        exact = [r for r in rows if (r["title"] or "").strip().casefold() == (title or "").strip().casefold()
                 and (r["artist"] or "").strip().casefold() == (artist or "").strip().casefold()]
        return max((r["duration_s"] for r in exact if r["duration_s"] is not None), default=None)

    @synchronized
    def tracks_to_enrich(self, playlist_id) -> list:
        """Tracks in this playlist still missing genre or year (NULL or blank), in playlist order.
        Blank counts as missing so re-running enrichment retries tracks that didn't fully resolve."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist FROM playlist_tracks pt "
            "JOIN catalog_tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? "
            "AND (t.genre IS NULL OR t.genre = '' OR t.mb_year IS NULL OR t.mb_year = '') "
            "ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "artist": r["artist"]}
                for r in rows]

    @synchronized
    def album_tracks_to_enrich(self, album_browse_id) -> list:
        """A saved album's folded-in tracks still missing genre or year, the album-scoped twin of
        tracks_to_enrich, so the same enrich runners work over an album."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist FROM catalog_tracks t JOIN album_track_links a ON a.entry_id=t.id WHERE a.browse_id=? "
            "AND (t.genre IS NULL OR t.genre = '' OR t.mb_year IS NULL OR t.mb_year = '') "
            "ORDER BY t.id", (album_browse_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "artist": r["artist"]}
                for r in rows]

    # A track still has work when anything is missing, INCLUDING its cover: YouTube serves a generic
    # grey disc rather than nothing when it has no art, so a track can be complete on genre/year/audio
    # and still have no real cover. Without these clauses the waterfall would never revisit it and the
    # cover could never arrive. `_` is a LIKE wildcard, hence the ESCAPE.
    _NO_ART = (r"t.thumbnail IS NULL OR t.thumbnail='' "
               r"OR t.thumbnail LIKE '%cover\_track\_default%' ESCAPE '\' "
               r"OR t.thumbnail LIKE '%cover\_album\_default%' ESCAPE '\'")
    _NEEDS = ("t.genre IS NULL OR t.genre='' OR t.mb_year IS NULL OR t.mb_year='' "
              "OR t.bpm IS NULL OR t.energy IS NULL OR t.danceability IS NULL "
              "OR " + _NO_ART)

    @synchronized
    def tracks_for_waterfall(self, playlist_id) -> list:
        """Playlist tracks the waterfall still has work for (missing genre, year, or any of the
        core audio features) in playlist order. Carries mb_recording_id so AcousticBrainz can key
        off an already-resolved MBID."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id, t.thumbnail FROM playlist_tracks pt "
            f"JOIN catalog_tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? AND ({self._NEEDS}) "
            "ORDER BY pt.position", (playlist_id,)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def track_for_waterfall(self, track_id) -> dict | None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        """One track for an explicit user-requested lookup, even if other metadata is complete."""
        row = self.conn.execute(
            "SELECT id, recording_id, identity_key, video_id, title, artist, mb_recording_id, thumbnail, duration_s FROM catalog_tracks WHERE id=?",
            (track_id,)).fetchone()
        return dict(row) if row is not None else None

    @synchronized
    def album_tracks_for_waterfall(self, album_browse_id) -> list:
        """A saved album's folded-in tracks the waterfall still has work for, album-scoped twin of
        tracks_for_waterfall."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id, t.thumbnail FROM catalog_tracks t "
            "JOIN album_track_links a ON a.entry_id=t.id "
            f"WHERE a.browse_id=? AND ({self._NEEDS}) ORDER BY t.id", (album_browse_id,)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def set_track_art(self, track_id, url, *, source='provider') -> None:
        """Persist provider fallback using the same artwork precedence as ingestion."""
        with self.conn:
            catalog.update_artwork(self.conn, track_id, url, source=source)

    @synchronized
    def set_track_genre(self, track_id, genre) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        # manual override: set exactly what the user chose (may be blank to clear)
        catalog.choose_field(self.conn,track_id,'genre',genre or '')
        self.conn.commit()

    @synchronized
    def set_track_year(self, track_id, year) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        # manual override: set exactly what the user typed (may be blank to clear)
        catalog.choose_field(self.conn,track_id,'mb_year',str(year) if year is not None else '')
        self.conn.commit()

    @synchronized
    def set_track_title(self, track_id, title) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        # manual fix: overwrite the live title in place so downstream consumers use it. Never blank.
        title = (title or "").strip()
        if not title:
            return
        catalog.choose_field(self.conn,track_id,'title',title)
        self.conn.commit()

    @synchronized
    def set_track_artist(self, track_id, artist) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        # manual fix: overwrite the live artist in place. Never blank.
        artist = (artist or "").strip()
        if not artist:
            return
        catalog.choose_field(self.conn,track_id,'artist',artist)
        self.conn.commit()

    @synchronized
    def reset_track_title(self, track_id) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        self.conn.execute("DELETE FROM entry_field_choices WHERE entry_id=? AND field='title'", (track_id,))
        # restore the original YouTube title (undo a manual fix).
        self.conn.execute("UPDATE catalog_tracks SET title=orig_title WHERE id=? AND orig_title IS NOT NULL",
                          (track_id,))
        self.conn.commit()

    @synchronized
    def reset_track_artist(self, track_id) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        self.conn.execute("DELETE FROM entry_field_choices WHERE entry_id=? AND field='artist'", (track_id,))
        self.conn.execute("UPDATE catalog_tracks SET artist=orig_artist WHERE id=? AND orig_artist IS NOT NULL",
                          (track_id,))
        self.conn.commit()

    @synchronized
    def tracks_missing_genre(self, playlist_id) -> list:
        """Playlist tracks with no genre yet (for Last.fm genre enrichment), in playlist order."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist FROM playlist_tracks pt "
            "JOIN catalog_tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? "
            "AND (t.genre IS NULL OR t.genre = '') ORDER BY pt.position", (playlist_id,)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "artist": r["artist"]}
                for r in rows]

    @synchronized
    def set_track_enrichment(self, track_id, genre, year) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        # fill-only: set a field just when it's currently blank. So enrichment fills gaps and never
        # overwrites what you already have. MusicBrainz and Last.fm each top up the other's misses.
        self.conn.execute(
            "UPDATE catalog_tracks SET "
            "  genre = CASE WHEN (genre IS NULL OR genre='') AND ? <> '' THEN ? ELSE genre END, "
            "  mb_year = CASE WHEN (mb_year IS NULL OR mb_year='') AND ? <> '' THEN ? ELSE mb_year END "
            "WHERE id=?",
            (genre or "", genre or "", year or "", year or "", track_id))
        self.conn.commit()

    @synchronized
    def get_track_enrichment(self, track_id):
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Current (genre, year) for a track, used to report the effective value after a fill."""
        row = self.conn.execute("SELECT genre, mb_year FROM catalog_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return ("", "")
        return (row["genre"] or "", row["mb_year"] or "")

    @synchronized
    def set_track_audio(self, track_id, bpm=None, energy=None, danceability=None,
                        music_key=None, music_scale=None, mood_happy=None, mood_sad=None,
                        mood_relaxed=None, mood_acoustic=None, instrumental=None,
                        loudness=None, dynamic_complexity=None,
                        popularity=None, gain=None, label=None) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
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
        catalog.replace_hints(self.conn,track_id,updates)
        # column names come from the fixed literal dict above (not user input) -> safe to inline
        sets = ", ".join(f"{c} = CASE WHEN {c} IS NULL THEN ? ELSE {c} END" for c in updates)
        self.conn.execute(f"UPDATE catalog_tracks SET {sets} WHERE id=?",
                          list(updates.values()) + [track_id])
        self.conn.commit()

    @synchronized
    def get_track_audio(self, track_id):
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Current (bpm, energy, danceability) for a track, each float or None."""
        row = self.conn.execute(
            "SELECT bpm, energy, danceability FROM catalog_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            return (None, None, None)
        return (row["bpm"], row["energy"], row["danceability"])

    @synchronized
    def set_track_mbid(self, track_id, mbid) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        # fill-only: keep the first MBID we resolve for a track.
        if not mbid:
            return
        self.conn.execute(
            "UPDATE catalog_tracks SET mb_recording_id = CASE "
            "WHEN mb_recording_id IS NULL OR mb_recording_id='' THEN ? ELSE mb_recording_id END "
            "WHERE id=?", (mbid, track_id))
        self.conn.commit()

    @synchronized
    def tracks_missing_audio(self, playlist_id) -> list[dict]:
        """Playlist tracks still missing any audio feature (bpm/energy/danceability), in order.
        Carries mb_recording_id so AcousticBrainz can key off it without a second query."""
        rows = self.conn.execute(
            "SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id FROM playlist_tracks pt "
            "JOIN catalog_tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? "
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
                "SELECT id FROM catalog_tracks WHERE video_id=? ORDER BY id DESC LIMIT 1", (vid,)).fetchone()
            if row is not None:
                out[vid] = row["id"]
        return out

    @synchronized
    def materialized_album_ids(self) -> set:
        """Releases with any library track. This does not prove full materialization."""
        return {r["browse_id"] for r in self.conn.execute(
            "SELECT DISTINCT a.browse_id FROM album_track_links a JOIN library_memberships m ON m.entry_id=a.entry_id")}
