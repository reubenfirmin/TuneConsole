"""Catalog/content projections for recommendation consumers.

Listening evidence and training baskets belong to RecEvidenceRepo, exclusions to
RecEligibilityRepo, and ranked library candidates to RecCandidatesRepo. This owner
only projects recorded library metadata; it does not forward those other APIs.
"""
from yt_playlist.core import album_memberships

from yt_playlist.library.rating_policy import LIKED_EXISTS
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read
from yt_playlist.util.years import decade as _decade


class RecQueryRepo(Repo):
    # --- library primitives ---
    @synchronized
    def key_for_video(self, video_id):
        """identity_key for a video_id (track rows carry video_id; the model is keyed by identity_key)."""
        r = self.conn.execute(
            "SELECT identity_key FROM tracks WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
        return r["identity_key"] if r else None

    @lookup_read
    def track_genres(self, keys) -> dict:
        """{identity_key: genre} for the given keys that have a genre (for labelling clusters)."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["genre"] for r in self.conn.execute(
            f"SELECT identity_key, genre FROM catalog_tracks WHERE identity_key IN ({qs}) AND genre<>''", keys)}

    @lookup_read
    def track_decades(self, keys) -> dict:
        """{identity_key: '1990'} for keys whose mb_year is a 4-digit year (floored to its decade)."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        out = {}
        for r in self.conn.execute(
            f"SELECT identity_key k, MIN(mb_year) y FROM catalog_tracks "
            f"WHERE identity_key IN ({qs}) AND mb_year<>'' GROUP BY identity_key", keys):
            d = _decade(r["y"], as_str=True)
            if d is not None:
                out[r["k"]] = d
        return out

    @synchronized
    def track_last_played(self, keys) -> dict:
        """{identity_key: newest play timestamp} for the given keys with any play history.
        Keys never played are absent (caller treats absence as 'coldest')."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key k,MAX(at) last FROM listening_activity "
            f"WHERE identity_key IN ({qs}) GROUP BY identity_key", keys).fetchall()
        return {r["k"]: r["last"] for r in rows}

    @synchronized
    def track_artists(self, keys) -> dict:
        """{identity_key: artist} for the given keys that have an artist."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["artist"] for r in self.conn.execute(
            f"SELECT identity_key, MIN(artist) artist FROM catalog_tracks "
            f"WHERE identity_key IN ({qs}) AND artist<>'' GROUP BY identity_key", keys)}

    @synchronized
    def track_popularity(self, keys) -> dict:
        """{identity_key: popularity} (Deezer rank) for keys carrying a popularity value; absent = unknown.
        Powers the 'pop:' mainstream axis (#43). MAX over duplicate video rows picks the most-popular variant."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["p"] for r in self.conn.execute(
            f"SELECT identity_key, MAX(popularity) p FROM tracks "
            f"WHERE identity_key IN ({qs}) AND popularity IS NOT NULL GROUP BY identity_key", keys)}

    @synchronized
    def year_play_weights(self, *, owned_weight):
        """Supplied weighting over distinct song/year pairs, aggregated in SQLite."""
        rows = self.conn.execute(
            "WITH tp AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key, plays c "
            "FROM listening_day_counts), "
            "     songs AS (SELECT DISTINCT identity_key, mb_year FROM tracks WHERE mb_year<>'') "
            "SELECT s.mb_year y, SUM(? + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.mb_year", (owned_weight,)).fetchall()
        return [dict(row) for row in rows]

    @snapshot_read
    def tracks_total(self) -> int:
        """Total tracks in the library (taste-model coverage denominator)."""
        return self.conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]

    @synchronized
    def owned_albums(self) -> set:
        """Canonical release IDs represented in the library or saved."""
        rows = self.conn.execute(
            f"SELECT at.album_id a FROM {self.album_tracks_table} at JOIN tracks t ON t.id=at.entry_id "
            f"UNION SELECT album_id FROM {album_memberships.read_relation(self.conn)}").fetchall()
        return {r["a"] for r in rows if r["a"]}

    @synchronized
    def library_keys(self) -> set:
        """All track identity_keys in the library, to filter 'fresh' (unowned) discovery."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key FROM tracks")}

    @snapshot_read
    def library_artist_ids(self) -> list:
        return [r[0] for r in self.conn.execute(
            f'SELECT DISTINCT ta.artist_id FROM {self.entry_artists_table} ta JOIN tracks t ON t.id=ta.entry_id')]

    @snapshot_read
    def tracks_by_artists(self, artist_ids) -> list:
        """Playable owned recordings credited to requested artists, on a private reader.

        Constrain recordings through the credit index before choosing each one's
        first playable library entry. Credits and display fields come from that
        same entry, preserving the playable-library representative contract.
        """
        if not artist_ids:
            return []
        wanted = sorted(set(artist_ids))
        out = {}
        for offset in range(0, len(wanted), 400):
            batch = wanted[offset:offset + 400]
            qs = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                "WITH requested AS MATERIALIZED ("
                f"SELECT DISTINCT e.recording_id FROM {self.entry_artists_table} ta "
                "JOIN catalog_entries e ON e.id=ta.entry_id "
                f"WHERE ta.artist_id IN ({qs})) "
                "SELECT t.identity_key k,t.title,t.artist,t.album,t.video_id vid,t.thumbnail thumb,t.genre "
                "FROM tracks t WHERE t.title<>'' AND t.id IN ("
                "SELECT MIN(e.id) FROM requested r CROSS JOIN catalog_entries e ON e.recording_id=r.recording_id "
                "JOIN library_memberships m ON m.entry_id=e.id "
                "JOIN youtube_items y ON y.video_id=e.video_id "
                "WHERE COALESCE(y.available,1)<>0 GROUP BY e.recording_id) "
                f"AND EXISTS (SELECT 1 FROM {self.entry_artists_table} ta WHERE ta.entry_id=t.id "
                f"AND ta.artist_id IN ({qs}))", [*batch, *batch])
            for r in rows:
                out[r["k"]] = {"key": r["k"], "title": r["title"], "artist": r["artist"],
                               "album": r["album"] or "", "video_id": r["vid"], "thumbnail": r["thumb"],
                               "genre": r["genre"] or ""}
        return [out[key] for key in sorted(out)]

    @synchronized
    def track_content(self) -> dict:
        """{identity_key: (genre, year4)} for tracks with either categorical input."""
        rows = self.conn.execute(
            "SELECT identity_key k, MIN(genre) g, MIN(mb_year) y FROM tracks "
            "WHERE genre<>'' OR mb_year<>'' GROUP BY identity_key").fetchall()
        out = {}
        for r in rows:
            y = r["y"][:4] if (r["y"] and r["y"][:4].isdigit()) else None
            out[r["k"]] = (r["g"], y)
        return out

    # Audio features that feed the content model's "sounds-like" block.
    AUDIO_COLS = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                  "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                  "music_key", "music_scale")

    @synchronized
    def content_inputs_by_keys(self, keys) -> dict:
        """Content-model inputs for changed recordings, without reading the vector corpus."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        audio = ", ".join(f"MIN({c}) AS {c}" for c in self.AUDIO_COLS)
        return {r["identity_key"]: {
            "genre": r["genre"], "year": r["year"],
            "audio": {c: r[c] for c in self.AUDIO_COLS if r[c] not in (None, "")},
        } for r in self.conn.execute(
            f"SELECT identity_key, MIN(NULLIF(genre,'')) AS genre, MIN(NULLIF(mb_year,'')) AS year, "
            f"{audio} FROM tracks WHERE identity_key IN ({qs}) GROUP BY identity_key", keys)}

    @synchronized
    def artist_genre_counts(self):
        """Distinct recording counts per credited artist and recorded genre."""
        rows = self.conn.execute(
            "SELECT a.id artist_id,a.name artist,t.genre, COUNT(DISTINCT t.identity_key) c "
            f"FROM tracks t JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id JOIN artists a ON a.id=ta.artist_id "
            "WHERE t.genre<>'' GROUP BY a.id,t.genre").fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def track_audio_features(self) -> dict:
        """{identity_key: {feature: value}} for tracks carrying any audio metadata. Continuous
        AcousticBrainz/Deezer features plus the categorical music_key / music_scale. One row per
        identity_key (MIN over duplicate video rows); absent features are simply omitted from the
        per-key dict, so the content builder can z-score over only what's present."""
        cols = self.AUDIO_COLS
        sel = ", ".join(f"MIN({c}) {c}" for c in cols)
        where = " OR ".join(f"{c} IS NOT NULL" for c in cols)
        rows = self.conn.execute(
            f"SELECT identity_key k, {sel} FROM tracks WHERE {where} GROUP BY identity_key").fetchall()
        out = {}
        for r in rows:
            d = {c: r[c] for c in cols if r[c] is not None and r[c] != ""}
            if d:
                out[r["k"]] = d
        return out

    @lookup_read
    def tracks_by_keys(self, keys) -> dict:
        """Display metadata for a set of identity_keys: {key: {title, artist, album, video_id, thumbnail}}."""
        keys = list(dict.fromkeys(keys))
        result = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            qs = ",".join("?" for _ in batch)
            # Select the same first playable owned entry as playable_library_tracks,
            # but constrain recordings BEFORE grouping. The view grouped the whole
            # library even when Home needed only a handful of display rows.
            rows = self.conn.execute(
                "SELECT t.identity_key k,t.title,t.artist,t.album,t.video_id vid,t.thumbnail thumb "
                "FROM tracks t WHERE t.id IN ("
                "SELECT MIN(e.id) FROM recordings r JOIN catalog_entries e ON e.recording_id=r.id "
                "JOIN library_memberships m ON m.entry_id=e.id JOIN youtube_items y ON y.video_id=e.video_id "
                f"WHERE r.identity_key IN ({qs}) AND COALESCE(y.available,1)<>0 GROUP BY r.id)", batch).fetchall()
            result.update({r["k"]: {"title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                                    "video_id": r["vid"], "thumbnail": r["thumb"]} for r in rows})
        return result

    @synchronized
    def group_candidates(self, keys):
        """Current owned display and eligibility fields for prepared group pools.

        Resolve the playable entry before reading its catalog projection, once
        per recording, rather than reloading metadata in separate pool passes.
        """
        keys = list(dict.fromkeys(keys))
        result = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            qs = ','.join('?' for _ in batch)
            rows = self.conn.execute(
                'SELECT t.identity_key,t.title,t.artist,t.album,t.video_id,t.thumbnail,t.genre,t.duration_s '
                'FROM tracks t WHERE t.id IN ('
                'SELECT MIN(e.id) FROM recordings r JOIN catalog_entries e ON e.recording_id=r.id '
                'JOIN library_memberships m ON m.entry_id=e.id JOIN youtube_items y ON y.video_id=e.video_id '
                f'WHERE r.identity_key IN ({qs}) AND COALESCE(y.available,1)<>0 GROUP BY r.id)', batch).fetchall()
            result.update({row['identity_key']: dict(row) for row in rows})
        return result

    @synchronized
    def library_genre_counts(self):
        """Distinct recording counts per nonempty recorded genre."""
        rows = self.conn.execute(
            "SELECT genre, COUNT(DISTINCT identity_key) n FROM tracks "
            "WHERE genre IS NOT NULL AND genre<>'' GROUP BY genre").fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def keys_for_genres(self, genres):
        """Query only the genre labels selected by the caller."""
        genres = list(dict.fromkeys(genres))
        out = set()
        for offset in range(0, len(genres), 400):
            page = genres[offset:offset + 400]
            qs = ','.join('?' for _ in page)
            out.update(row[0] for row in self.conn.execute(
                f'SELECT DISTINCT identity_key FROM tracks WHERE genre IN ({qs})', page))
        return out

    @snapshot_read
    def top_played_keys(self, limit=10) -> list[str]:
        """Account keys of your most-played songs (for seeding taste-neighbourhood recs)."""
        rows = self.conn.execute(
            "SELECT identity_key k, plays c FROM listening_day_counts "
            "ORDER BY c DESC LIMIT ?", (limit,)).fetchall()
        return [r["k"] for r in rows]

    @synchronized
    def play_counts(self) -> dict:
        """{identity_key: account listening-day count}. A key absent from the map has
        never been played (count 0), the primary signal for the Catalog card (your under-played
        catalog)."""
        return {r["identity_key"]: r["c"] for r in self.conn.execute(
            "SELECT identity_key, plays c FROM listening_day_counts").fetchall()}

    @synchronized
    def tagged_facets(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT DISTINCT identity_key,genre,mb_year FROM tracks WHERE genre<>'' OR mb_year<>''")]

    @synchronized
    def facet_values(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT DISTINCT genre,mb_year FROM tracks WHERE genre<>'' OR mb_year<>''")]

    @synchronized
    def library_songs(self, *, maximum_duration) -> list[dict]:
        """Every playable song you own, once per identity_key: {key, video_id, title, artist, album,
        thumbnail, duration, genre, year, liked}.

        The whole collection in one query (~30ms for 4k songs), so a feature that wants to choose
        from everything you have doesn't need to sample it first and then discover the sample was
        missing what you asked for.

        DJ mixes and live sets use the shared rec_eligibility track length limit:
        a 2.5-hour festival set is not a track, and one of them would swallow a
        whole playlist's duration budget by itself."""
        rows = self.conn.execute(
            "SELECT t.identity_key k, MIN(t.video_id) vid, MIN(t.title) title, MIN(t.artist) artist, "
            "       MIN(t.album) album, MIN(t.thumbnail) thumb, MIN(t.duration_s) dur, "
            "       MIN(NULLIF(t.genre,'')) genre, MIN(NULLIF(t.mb_year,'')) yr, "
            f"      MAX(CASE WHEN {LIKED_EXISTS} THEN 1 ELSE 0 END) liked "
            "FROM playable_library_tracks t WHERE t.video_id IS NOT NULL AND t.video_id<>'' "
            "  AND (t.duration_s IS NULL OR t.duration_s<=?) "
            "GROUP BY t.identity_key", (maximum_duration,)).fetchall()
        out = []
        for r in rows:
            year = str(r["yr"] or "")[:4]
            out.append({"key": r["k"], "video_id": r["vid"], "title": r["title"] or "",
                        "artist": r["artist"] or "", "album": r["album"] or "",
                        "thumbnail": r["thumb"], "duration": r["dur"],
                        "genre": r["genre"] or "", "year": int(year) if year.isdigit() else None,
                        "liked": bool(r["liked"])})
        return out

    @lookup_read
    def artist_metadata_for(self, artist_ids, *, per_artist):
        """Recorded metadata from a bounded sample of each exact artist credit."""
        result = {}
        for aid in set(artist_ids):
            rows = self.conn.execute(
                f'SELECT t.genre,t.mb_year FROM (SELECT entry_id FROM {self.entry_artists_table} '
                'WHERE artist_id=? ORDER BY entry_id LIMIT ?) a '
                'JOIN tracks t ON t.id=a.entry_id', (aid, per_artist)).fetchall()
            result[aid] = [dict(row) for row in rows]
        return result

    @synchronized
    def artist_metadata(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT artist,genre,mb_year FROM tracks WHERE artist<>'' AND (genre<>'' OR mb_year<>'')")]

    # --- genre distributions ---
    @synchronized
    def genre_distribution(self) -> dict:
        """{genre: track_count} over tagged tracks."""
        return {r["genre"]: r["c"] for r in self.conn.execute(
            "SELECT genre, COUNT(*) c FROM tracks WHERE genre<>'' GROUP BY genre")}

    @synchronized
    def genre_play_distribution(self, *, owned_weight) -> dict:
        """Supplied weighting over distinct song/genre pairs, aggregated in SQLite."""
        # Collapse to one row per (song, genre) FIRST: a song commonly has several `tracks` rows
        # (same identity_key, different video_id uploads). Without the DISTINCT, the LEFT JOIN below
        # would add (1 + plays) once per upload, multiplying a song's weight by its duplicate count.
        rows = self.conn.execute(
            "WITH tp AS MATERIALIZED (SELECT CAST(identity_key AS TEXT) identity_key, plays c "
            "FROM listening_day_counts), "
            "     songs AS (SELECT DISTINCT identity_key, genre FROM tracks WHERE genre <> '') "
            "SELECT s.genre g, SUM(? + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.genre", (owned_weight,)).fetchall()
        return {r["g"]: r["w"] for r in rows}

    @synchronized
    def playlist_track_genres(self, playlist_id) -> list[str]:
        """Non-empty genres of a playlist's tracks for taste context summaries."""
        return [r["g"] for r in self.conn.execute(
            "SELECT t.genre g FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? AND t.genre<>''", (playlist_id,))]
