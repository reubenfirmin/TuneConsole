"""Catalog/content projections for recommendation consumers.

Listening evidence and training baskets belong to RecEvidenceRepo, exclusions to
RecEligibilityRepo, and ranked library candidates to RecCandidatesRepo. This owner
only projects recorded library metadata; it does not forward those other APIs.
"""
from collections import Counter
from yt_playlist.core import artists

from yt_playlist.repos.base import LIKED_EXISTS, Repo, synchronized, snapshot_read
from yt_playlist.util import genre_map
from yt_playlist.util.years import decade as _decade


class RecQueryRepo(Repo):
    # --- library primitives ---
    @synchronized
    def key_for_video(self, video_id):
        """identity_key for a video_id (track rows carry video_id; the model is keyed by identity_key)."""
        r = self.conn.execute(
            "SELECT identity_key FROM tracks WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
        return r["identity_key"] if r else None

    @synchronized
    def track_genres(self, keys) -> dict:
        """{identity_key: genre} for the given keys that have a genre (for labelling clusters)."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["genre"] for r in self.conn.execute(
            f"SELECT identity_key, genre FROM catalog_tracks WHERE identity_key IN ({qs}) AND genre<>''", keys)}

    @synchronized
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
    def era_play_distribution(self) -> dict:
        """{decade: Σ(1 + play_count)} over dated tracks, deduped per song (mirrors genre version)."""
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, plays c FROM listening_day_counts), "
            "     songs AS (SELECT DISTINCT identity_key, mb_year FROM tracks WHERE mb_year<>'') "
            "SELECT s.mb_year y, SUM(1 + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.identity_key, s.mb_year").fetchall()
        out: dict = {}
        for r in rows:
            d = _decade(r["y"], as_str=True)
            if d is not None:
                out[d] = out.get(d, 0) + r["w"]
        return out

    @synchronized
    def tracks_total(self) -> int:
        """Total tracks in the library (taste-model coverage denominator)."""
        return self.conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]

    @synchronized
    def owned_albums(self) -> set:
        """Canonical release IDs represented in the library or saved."""
        rows = self.conn.execute(
            "SELECT at.album_id a FROM album_tracks at JOIN tracks t ON t.id=at.entry_id "
            "UNION SELECT album_id FROM album_library_memberships").fetchall()
        return {r["a"] for r in rows if r["a"]}

    @synchronized
    def library_keys(self) -> set:
        """All track identity_keys in the library, to filter 'fresh' (unowned) discovery."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key FROM tracks")}

    @snapshot_read
    def library_artists(self) -> set:
        """Normalized artist names already in the library, to exclude from new-artist discovery."""
        from yt_playlist.core import artists
        ids = [r[0] for r in self.conn.execute(
            'SELECT DISTINCT ta.artist_id FROM track_artists ta JOIN tracks t ON t.id=ta.entry_id')]
        return set(artists.public_keys(self.conn, ids).values())

    @snapshot_read
    def tracks_by_artists(self, artists) -> list:
        """Playable owned recordings credited to requested artists, on a private reader.

        Constrain recordings through the credit index before choosing each one's
        first playable library entry. Credits and display fields come from that
        same entry, preserving the playable-library representative contract.
        """
        from yt_playlist.core import artists as artist_catalog
        want = {artist_catalog.lookup(self.conn,a) for a in artists}
        want.discard(None)
        if not want:
            return []
        wanted = sorted(want)
        out = {}
        for offset in range(0, len(wanted), 400):
            batch = wanted[offset:offset + 400]
            qs = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                "WITH requested AS MATERIALIZED ("
                "SELECT DISTINCT e.recording_id FROM track_artists ta "
                "JOIN catalog_entries e ON e.id=ta.entry_id "
                f"WHERE ta.artist_id IN ({qs})) "
                "SELECT t.identity_key k,t.title,t.artist,t.album,t.video_id vid,t.thumbnail thumb,t.genre "
                "FROM tracks t WHERE t.title<>'' AND t.id IN ("
                "SELECT MIN(e.id) FROM requested r CROSS JOIN catalog_entries e ON e.recording_id=r.recording_id "
                "JOIN library_memberships m ON m.entry_id=e.id "
                "JOIN youtube_items y ON y.video_id=e.video_id "
                "WHERE COALESCE(y.available,1)<>0 GROUP BY e.recording_id) "
                "AND EXISTS (SELECT 1 FROM track_artists ta WHERE ta.entry_id=t.id "
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
    def artist_genres(self, *, canonical=False) -> dict:
        """{artist: dominant genre} over the library, the most common non-empty genre per artist.
        Lets outward discovery tag a new ALBUM by its (owned) artist's known genre, no network."""
        rows = self.conn.execute(
            "SELECT a.id artist_id,a.name artist,t.genre, COUNT(DISTINCT t.identity_key) c "
            "FROM tracks t JOIN track_artists ta ON ta.entry_id=t.id JOIN artists a ON a.id=ta.artist_id "
            "WHERE t.genre<>'' GROUP BY a.id,t.genre").fetchall()
        best = {}
        for r in rows:
            a = artists.token(r['artist_id']) if canonical or artists.lookup(self.conn,r['artist']) is None else r['artist']
            if a not in best or r["c"] > best[a][1]:
                best[a] = (r["genre"], r["c"])
        return {a: g for a, (g, _) in best.items()}

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

    @synchronized
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
    def library_genre_families(self) -> list[dict]:
        """Genre families present in the library, each with its track count, the option list for the
        Clusters genre-family filter (#29). Untagged tracks contribute nothing. Sorted most-common
        first, then alphabetically."""
        fams = {}
        for r in self.conn.execute(
                "SELECT genre, COUNT(DISTINCT identity_key) n FROM tracks "
                "WHERE genre IS NOT NULL AND genre<>'' GROUP BY genre"):
            fam = genre_map.family(r["genre"])
            fams[fam] = fams.get(fam, 0) + r["n"]
        return [{"family": f, "n": n} for f, n in
                sorted(fams.items(), key=lambda kv: (-kv[1], kv[0]))]

    @synchronized
    def keys_in_families(self, families) -> set:
        """Identity_keys whose genre maps into one of `families` (#29 whitelist). Untagged tracks are
        never included. A track with no genre can't be vouched into a restricted cluster."""
        fams = set(families)
        if not fams:
            return set()
        return {r["k"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key k, genre FROM tracks WHERE genre IS NOT NULL AND genre<>''")
            if genre_map.family(r["genre"]) in fams}

    @synchronized
    def library_genres(self) -> list[dict]:
        """Individual genres (sub-genres) present in the library, each with its family and track count,
        the fine-grained options for the Clusters genre filter (#29), alongside the coarse families.
        Sorted most-common first, then alphabetically."""
        rows = self.conn.execute(
            "SELECT genre, COUNT(DISTINCT identity_key) n FROM tracks "
            "WHERE genre IS NOT NULL AND genre<>'' GROUP BY genre").fetchall()
        return [{"genre": r["genre"], "family": genre_map.family(r["genre"]), "n": r["n"]}
                for r in sorted(rows, key=lambda r: (-r["n"], r["genre"].lower()))]

    @synchronized
    def keys_in_genre_selection(self, tokens) -> set:
        """Identity_keys allowed by a Clusters genre whitelist (#29) where each token may be EITHER a
        genre family OR a specific genre (sub-genre). A track qualifies if its genre matches a token
        exactly, or its family matches a token. Untagged tracks are never included."""
        toks = {t.strip().lower() for t in tokens if t and t.strip()}
        if not toks:
            return set()
        out = set()
        for r in self.conn.execute(
                "SELECT DISTINCT identity_key k, genre FROM tracks WHERE genre IS NOT NULL AND genre<>''"):
            g = r["genre"].strip().lower()
            if g in toks or genre_map.family(r["genre"]).lower() in toks:
                out.add(r["k"])
        return out

    @snapshot_read
    def top_played_keys(self, limit=10) -> list[str]:
        """Identity keys of your most-played songs (for seeding taste-neighbourhood recs)."""
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
            "SELECT identity_key, plays c FROM listening_day_counts")}

    @synchronized
    def play_weighted_facets(self, plays) -> dict:
        """Summarize Home's tagged facets once using this generation's play counts.

        Uploads can disagree about genre/year. Deduplicate each song/facet pair
        independently, exactly as the separate genre and era queries do.
        """
        genres, years = set(), set()
        for row in self.conn.execute(
                "SELECT DISTINCT identity_key,genre,mb_year FROM tracks WHERE genre<>'' OR mb_year<>''").fetchall():
            if row['genre']:
                genres.add((row['identity_key'], row['genre']))
            if row['mb_year']:
                years.add((row['identity_key'], row['mb_year']))
        genre_plays, era_plays = {}, {}
        for key, genre in sorted(genres, key=lambda pair: (str(pair[1]), pair[0])):
            genre_plays[genre] = genre_plays.get(genre, 0) + 1 + plays.get(key, 0)
        for key, year in sorted(years, key=lambda pair: (pair[0], str(pair[1]))):
            decade = _decade(year, as_str=True)
            if decade is not None:
                era_plays[decade] = era_plays.get(decade, 0) + 1 + plays.get(key, 0)
        return {'genres': genre_plays, 'eras': era_plays}

    @synchronized
    def library_songs(self) -> list[dict]:
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
            f"  AND {self._store.rec_eligibility.track_length_clause('t.duration_s')} "
            "GROUP BY t.identity_key").fetchall()
        out = []
        for r in rows:
            year = str(r["yr"] or "")[:4]
            out.append({"key": r["k"], "video_id": r["vid"], "title": r["title"] or "",
                        "artist": r["artist"] or "", "album": r["album"] or "",
                        "thumbnail": r["thumb"], "duration": r["dur"],
                        "genre": r["genre"] or "", "year": int(year) if year.isdigit() else None,
                        "liked": bool(r["liked"])})
        return out

    @synchronized
    def artist_genre_years_for(self, names, *, per_artist=64):
        """Metadata fallback from a bounded sample of each selected artist's tracks."""
        result = {}
        for name in set(names):
            # Description fallback uses exact recorded credits. A missing credit
            # must not trigger lookup's normalized scan of every artist alias.
            aid = artists.lookup(self.conn, name, exact=True)
            if aid is None:
                continue
            rows = self.conn.execute(
                'SELECT t.genre,t.mb_year FROM (SELECT entry_id FROM track_artists '
                'WHERE artist_id=? ORDER BY entry_id LIMIT ?) a '
                'JOIN tracks t ON t.id=a.entry_id', (aid, per_artist)).fetchall()
            genres = Counter(r['genre'] for r in rows if r['genre'])
            years = [int(str(r['mb_year'])[:4]) for r in rows if str(r['mb_year'] or '')[:4].isdigit()]
            result[name] = {'genre': genres.most_common(1)[0][0] if genres else '',
                            'year': min(years) if years else None}
        return result

    @synchronized
    def artist_genre_years(self) -> dict:
        """{artist: {"genre": g, "year": y}} inferred from that artist's TAGGED tracks: their most
        common genre and earliest known year. A per-artist stand-in for tracks enrichment hasn't
        reached yet - most libraries are tagged in patches, and an untagged track by an artist you
        have five tagged tracks for is not really unknown. Artists with nothing tagged are absent."""
        genres, years = {}, {}
        for r in self.conn.execute(
                "SELECT artist, genre, mb_year FROM tracks "
                "WHERE artist<>'' AND (genre<>'' OR mb_year<>'')"):
            if r["genre"]:
                genres.setdefault(r["artist"], Counter())[r["genre"]] += 1
            y = str(r["mb_year"] or "")[:4]
            if y.isdigit():
                years[r["artist"]] = min(int(y), years.get(r["artist"], 9999))
        out = {}
        for artist in set(genres) | set(years):
            counts = genres.get(artist)
            out[artist] = {"genre": counts.most_common(1)[0][0] if counts else "",
                           "year": years.get(artist)}
        return out

    # --- genre distributions ---
    @synchronized
    def genre_distribution(self) -> dict:
        """{genre: track_count} over tagged tracks."""
        return {r["genre"]: r["c"] for r in self.conn.execute(
            "SELECT genre, COUNT(*) c FROM tracks WHERE genre<>'' GROUP BY genre")}

    @synchronized
    def genre_play_distribution(self) -> dict:
        """{genre: Σ (1 + play_count)} over tagged tracks, play-weighted so a barely-played
        context counts toward breadth far less than one you actually listen to (the +1 keeps
        owned-but-unplayed tracks from vanishing entirely)."""
        # Collapse to one row per (song, genre) FIRST: a song commonly has several `tracks` rows
        # (same identity_key, different video_id uploads). Without the DISTINCT, the LEFT JOIN below
        # would add (1 + plays) once per upload, multiplying a song's weight by its duplicate count.
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, plays c FROM listening_day_counts), "
            "     songs AS (SELECT DISTINCT identity_key, genre FROM tracks WHERE genre <> '') "
            "SELECT s.genre g, SUM(1 + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.genre").fetchall()
        return {r["g"]: r["w"] for r in rows}

    @synchronized
    def playlist_track_genres(self, playlist_id) -> list[str]:
        """Non-empty genres of a playlist's tracks for taste context summaries."""
        return [r["g"] for r in self.conn.execute(
            "SELECT t.genre g FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? AND t.genre<>''", (playlist_id,))]
