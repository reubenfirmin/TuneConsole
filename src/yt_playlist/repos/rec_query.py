"""RecQueryRepo — read-only library queries and recommendation candidate generators.

These are the rec engine's reads over the library (tracks / playlist_tracks / history_items):
library primitives (keys, genres, distributions), the generated-playlist quarantine, and the
candidate-surface generators (comfort / rotation / deep cuts / completion / enrichment). They're
grouped here because the generators all depend on the same exclusion logic (excluded_playlist_ids).
"""
from yt_playlist.repos.base import Repo, synchronized

# Auto-assigned group for playlists this app generates from recommendations. Anything in this group
# is quarantined from every taste signal (groupings/analysis/scores) until it "becomes highly
# played" — so the engine never feeds on its own suggestions. A generated playlist graduates either
# by being moved out of this group, or by its tracks accumulating real plays: total history plays
# >= GRADUATE_PLAYS_PER_TRACK x track_count (avg this many plays per track).
GENERATED_GROUP = "Generated"
GRADUATE_PLAYS_PER_TRACK = 2


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
            f"SELECT identity_key, genre FROM tracks WHERE identity_key IN ({qs}) AND genre<>''", keys)}

    @synchronized
    def track_decades(self, keys) -> dict:
        """{identity_key: '1990'} for keys whose mb_year is a 4-digit year (floored to its decade)."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        out = {}
        for r in self.conn.execute(
            f"SELECT identity_key k, MIN(mb_year) y FROM tracks "
            f"WHERE identity_key IN ({qs}) AND mb_year<>'' GROUP BY identity_key", keys):
            y = r["y"][:4] if r["y"] else ""
            if y.isdigit():
                out[r["k"]] = str(int(y) // 10 * 10)
        return out

    @synchronized
    def track_artists(self, keys) -> dict:
        """{identity_key: artist} for the given keys that have an artist."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: r["artist"] for r in self.conn.execute(
            f"SELECT identity_key, MIN(artist) artist FROM tracks "
            f"WHERE identity_key IN ({qs}) AND artist<>'' GROUP BY identity_key", keys)}

    @synchronized
    def era_play_distribution(self) -> dict:
        """{decade: Σ(1 + play_count)} over dated tracks, deduped per song (mirrors genre version)."""
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            "     songs AS (SELECT DISTINCT identity_key, mb_year FROM tracks WHERE mb_year<>'') "
            "SELECT s.mb_year y, SUM(1 + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.identity_key, s.mb_year").fetchall()
        out: dict = {}
        for r in rows:
            y = (r["y"] or "")[:4]
            if y.isdigit():
                d = str(int(y) // 10 * 10)
                out[d] = out.get(d, 0) + r["w"]
        return out

    @synchronized
    def tracks_total(self) -> int:
        """Total tracks in the library (taste-model coverage denominator)."""
        return self.conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]

    @synchronized
    def owned_albums(self) -> set:
        """Lowercased album titles already in the library or saved — to filter outward discovery."""
        rows = self.conn.execute(
            "SELECT LOWER(album) a FROM tracks WHERE album<>'' "
            "UNION SELECT LOWER(title) FROM saved_albums").fetchall()
        return {r["a"] for r in rows if r["a"]}

    @synchronized
    def library_keys(self) -> set:
        """All track identity_keys in the library — to filter 'fresh' (unowned) discovery."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key FROM tracks")}

    @synchronized
    def library_artists(self) -> set:
        """Normalized artist names already in the library — to exclude from new-artist discovery."""
        from yt_playlist.matching import normalize
        rows = self.conn.execute("SELECT DISTINCT artist FROM tracks WHERE artist<>''").fetchall()
        return {normalize(r["artist"]) for r in rows}

    @synchronized
    def saved_album_ids(self) -> set:
        return {r["browse_id"] for r in self.conn.execute(
            "SELECT browse_id FROM saved_albums")}

    @synchronized
    def track_content(self) -> dict:
        """{identity_key: (genre, year4)} for tagged tracks — features for the content→embedding map."""
        rows = self.conn.execute(
            "SELECT identity_key k, MIN(genre) g, MIN(mb_year) y FROM tracks "
            "WHERE genre<>'' GROUP BY identity_key").fetchall()
        out = {}
        for r in rows:
            y = r["y"][:4] if (r["y"] and r["y"][:4].isdigit()) else None
            out[r["k"]] = (r["g"], y)
        return out

    @synchronized
    def tracks_by_keys(self, keys) -> dict:
        """Display metadata for a set of identity_keys: {key: {title, artist, album, video_id, thumbnail}}."""
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT identity_key k, MIN(title) title, MIN(artist) artist, MIN(album) album, "
            f"       MIN(video_id) vid, MIN(thumbnail) thumb FROM tracks "
            f"WHERE identity_key IN ({qs}) GROUP BY identity_key", keys).fetchall()
        return {r["k"]: {"title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                         "video_id": r["vid"], "thumbnail": r["thumb"]} for r in rows}

    @synchronized
    def top_played_keys(self, limit=10) -> list[str]:
        """Identity keys of your most-played songs (for seeding taste-neighbourhood recs)."""
        rows = self.conn.execute(
            "SELECT identity_key k, COUNT(*) c FROM history_items GROUP BY identity_key "
            "ORDER BY c DESC LIMIT ?", (limit,)).fetchall()
        return [r["k"] for r in rows]

    # --- genre distributions / adjacency ---
    @synchronized
    def genre_distribution(self) -> dict:
        """{genre: track_count} over tagged tracks — feeds the taste-breadth/palette computation."""
        return {r["genre"]: r["c"] for r in self.conn.execute(
            "SELECT genre, COUNT(*) c FROM tracks WHERE genre<>'' GROUP BY genre")}

    @synchronized
    def genre_play_distribution(self) -> dict:
        """{genre: Σ (1 + play_count)} over tagged tracks — play-weighted so a barely-played
        context counts toward breadth/palette far less than one you actually listen to (the +1 keeps
        owned-but-unplayed tracks from vanishing entirely)."""
        # Collapse to one row per (song, genre) FIRST: a song commonly has several `tracks` rows
        # (same identity_key, different video_id uploads). Without the DISTINCT, the LEFT JOIN below
        # would add (1 + plays) once per upload, multiplying a song's weight by its duplicate count.
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            "     songs AS (SELECT DISTINCT identity_key, genre FROM tracks WHERE genre <> '') "
            "SELECT s.genre g, SUM(1 + COALESCE(tp.c, 0)) w FROM songs s "
            "LEFT JOIN tp ON tp.identity_key = s.identity_key GROUP BY s.genre").fetchall()
        return {r["g"]: r["w"] for r in rows}

    @synchronized
    def genre_cooccurrence(self) -> dict:
        """How often each unordered genre pair shares a playlist — the corpus adjacency signal.

        Returns {"pairs": {(g1,g2): count}, "occ": {genre: #playlists}}. Used to pull genres the
        user repeatedly playlists together closer than the static map alone (spec §2.1/§5.3).
        """
        from collections import Counter
        excl = self.excluded_playlist_ids()
        pl = {}
        for r in self.conn.execute(
            "SELECT pt.playlist_id pid, t.genre g FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id WHERE t.genre<>''"):
            if r["pid"] in excl:                         # generated playlists don't shape adjacency
                continue
            pl.setdefault(r["pid"], set()).add(r["g"])
        pairs, occ = Counter(), Counter()
        for genres in pl.values():
            gs = sorted(genres)
            for g in gs:
                occ[g] += 1
            for i in range(len(gs)):
                for j in range(i + 1, len(gs)):
                    pairs[(gs[i], gs[j])] += 1
        return {"pairs": dict(pairs), "occ": dict(occ)}

    @synchronized
    def playlist_track_genres(self, playlist_id) -> list[str]:
        """Non-empty genres of a playlist's tracks (for the genre-diversity stat)."""
        return [r["g"] for r in self.conn.execute(
            "SELECT t.genre g FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? AND t.genre<>''", (playlist_id,))]

    # --- generated-playlist quarantine (so the engine never feeds on its own suggestions) ---
    @synchronized
    def excluded_playlist_ids(self, factor=GRADUATE_PLAYS_PER_TRACK, group=GENERATED_GROUP) -> set:
        """DB ids of generated playlists that haven't graduated into the rec engine — to be hidden
        from every taste signal. A generated playlist (group == `group`) is excluded until it earns
        its way in: you move it out of the group (so it's no longer generated), OR its tracks rack up
        real plays — total history plays >= factor x track_count. An empty/never-played one stays out.
        """
        rows = self.conn.execute(
            "WITH gen AS (SELECT p.id id FROM playlists p "
            "  JOIN playlist_group g ON g.ytm=p.ytm_playlist_id WHERE g.name=:grp), "
            " plays AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            " graded AS (SELECT gen.id FROM gen "
            "   JOIN playlist_tracks pt ON pt.playlist_id=gen.id "
            "   JOIN tracks t ON t.id=pt.track_id "
            "   LEFT JOIN plays ON plays.identity_key=t.identity_key "
            "   GROUP BY gen.id HAVING SUM(COALESCE(plays.c,0)) >= :factor*COUNT(*)) "
            "SELECT id FROM gen WHERE id NOT IN (SELECT id FROM graded)",
            {"grp": group, "factor": factor}).fetchall()
        return {r["id"] for r in rows}

    @synchronized
    def generated_track_keys(self, group=GENERATED_GROUP) -> set:
        """Identity_keys of every track already sitting in a generated-group playlist — so the
        recommendation lanes never re-offer songs you've just bundled into one (you saved it; don't
        suggest it back). Independent of graduation: once it's in a generated playlist, it's spoken for."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT t.identity_key FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id JOIN playlists p ON p.id=pt.playlist_id "
            "JOIN playlist_group g ON g.ytm=p.ytm_playlist_id WHERE g.name=?", (group,))}

    @synchronized
    def generated_only_unplayed_keys(self, factor=GRADUATE_PLAYS_PER_TRACK, group=GENERATED_GROUP) -> set:
        """Track keys that live ONLY in excluded generated playlists and have no plays — mirrors
        excluded_playlist_ids at the track level, so an unplayed generated song pollutes no embedding
        basket (album/artist/genre/year) either. Once it's played, or also lands in a real playlist,
        it counts again."""
        excl = self.excluded_playlist_ids(factor, group)
        if not excl:
            return set()
        qs = ",".join("?" * len(excl))
        rows = self.conn.execute(
            "WITH plays AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key) "
            "SELECT t.identity_key k FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "GROUP BY t.identity_key "
            f"HAVING SUM(CASE WHEN pt.playlist_id IN ({qs}) THEN 0 ELSE 1 END)=0 "
            "  AND COALESCE((SELECT c FROM plays WHERE identity_key=t.identity_key),0)=0",
            list(excl)).fetchall()
        return {r["k"] for r in rows}

    @synchronized
    def rec_baskets(self, max_playlist=120, max_album=30, max_session=120) -> list[list[str]]:
        """Co-occurrence baskets for the embedding model: playlists, albums, listening sessions.

        Catch-all playlists (more than max_playlist tracks) are excluded — they link everything to
        everything and only add noise. Live sets, full-performance uploads (UGC), and over-long
        "tracks" that are really DJ mixes/compilations are dropped too, since they co-occur with
        unrelated songs and blur the model. Each basket is a list of track identity_keys.
        """
        from yt_playlist import genre_map
        good = {r["k"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key k FROM tracks "
            "WHERE (video_type IS NULL OR video_type <> 'MUSIC_VIDEO_TYPE_UGC') "
            "AND (duration_s IS NULL OR duration_s <= 1200)")}
        good -= self.generated_only_unplayed_keys()      # quarantine unplayed generated songs
        excl = self.excluded_playlist_ids()              # ...and the generated playlists themselves
        pl_where = (" WHERE pt.playlist_id NOT IN (%s)" % ",".join(str(i) for i in excl)) if excl else ""
        out = []
        # structural baskets: tracks grouped by a shared column
        for grp, cap in (
            ("SELECT pt.playlist_id g, t.identity_key k FROM playlist_tracks pt "
             "JOIN tracks t ON t.id=pt.track_id" + pl_where, max_playlist),
            ("SELECT album g, identity_key k FROM tracks WHERE album<>''", max_album),
            ("SELECT artist g, identity_key k FROM tracks WHERE artist<>''", 50),
            ("SELECT snapshot_id g, identity_key k FROM history_items", max_session)):
            buckets = {}
            for r in self.conn.execute(grp):
                if r["k"] in good:
                    buckets.setdefault(r["g"], set()).add(r["k"])
            out += [list(s) for s in buckets.values() if 1 < len(s) <= cap]
        # content baskets: genre FAMILY (meta-genre map §2.1) and year decade
        fam, yr = {}, {}
        for r in self.conn.execute("SELECT genre, mb_year, identity_key k FROM tracks "
                                   "WHERE genre<>'' OR mb_year<>''"):
            if r["k"] not in good:
                continue
            if r["genre"]:
                fam.setdefault(genre_map.family(r["genre"]), set()).add(r["k"])
            if r["mb_year"] and r["mb_year"][:4].isdigit():
                yr.setdefault(int(r["mb_year"][:4]) // 10 * 10, set()).add(r["k"])
        out += [list(s) for s in fam.values() if 1 < len(s) <= 80]
        out += [list(s) for s in yr.values() if 1 < len(s) <= 80]
        return out

    # --- candidate-surface generators ---
    @synchronized
    def comfort_candidates(self, now, min_plays=4, recency_full_days=30, limit=50) -> list[dict]:
        """'Comfort listening': your high-rotation favorites, demoted the more recently you've heard
        them — reliable tracks you haven't reached for lately.

        play count = appearances across history snapshots; last_played = newest snapshot containing
        the song. Each track scores plays * min(1, days_since_last / recency_full_days): heavy
        rotation pushes it up, a recent play pulls it down (a track played today scores ~0). Only
        tracks with >= min_plays qualify, so this is always grounded in real listening — never-played
        library tracks don't surface here (that's resurface/explore territory).
        """
        full_secs = max(1.0, recency_full_days * 86400.0)
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key, COUNT(*) c, MAX(hs.taken_at) last "
            "  FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            "  GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(title) title, MIN(artist) artist, "
            "               MIN(album) album, MIN(video_id) vid, MIN(thumbnail) thumb "
            "               FROM tracks GROUP BY identity_key) "
            "SELECT n.identity_key k, n.title, n.artist, n.album, n.vid, n.thumb, "
            "       p.c plays, p.last last "
            "FROM names n JOIN plays p ON p.identity_key=n.identity_key "
            "WHERE n.title <> '' AND p.c >= :min_plays "
            "ORDER BY p.c * min(1.0, (:now - p.last) / :full_secs) DESC, p.last ASC LIMIT :limit",
            {"min_plays": min_plays, "now": now, "full_secs": full_secs, "limit": limit}).fetchall()
        return [{"key": r["k"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "last_played": r["last"]} for r in rows]

    @synchronized
    def more_like_rotation(self, seed_limit=40, limit=40) -> list[dict]:
        """Tracks that share a playlist with your most-played songs but that you barely play.

        Collaborative signal: 'because you listen to X, and these live alongside X in your
        playlists.' Seeds = your top-played songs; candidates = co-members of their playlists.
        """
        excl = self.excluded_playlist_ids()
        seed_excl = (" AND pt.playlist_id NOT IN (%s)" % ",".join(str(i) for i in excl)) if excl else ""
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            " seeds AS (SELECT identity_key k FROM tp ORDER BY c DESC LIMIT :seed_limit), "
            " seedpl AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id JOIN seeds s ON s.k=t.identity_key"
            f"            WHERE 1=1{seed_excl}), "
            " cand AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                 MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                 COUNT(DISTINCT pt.playlist_id) sp, COALESCE(MAX(tp.c),0) plays "
            "          FROM playlist_tracks pt JOIN seedpl ON seedpl.pid=pt.playlist_id "
            "          JOIN tracks t ON t.id=pt.track_id "
            "          LEFT JOIN tp ON tp.identity_key=t.identity_key "
            "          WHERE t.title<>'' GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sp, plays FROM cand "
            "WHERE key NOT IN (SELECT k FROM seeds) AND plays<=1 "
            "ORDER BY sp DESC, plays ASC, key LIMIT :limit",
            {"seed_limit": seed_limit, "limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "shared_playlists": r["sp"]} for r in rows]

    @synchronized
    def deep_cuts(self, limit=40) -> list[dict]:
        """The least-played track of each artist you play a lot — 'you love them, revisit this.'

        Content/affinity signal that needs no history depth: ranks artists by total plays,
        surfaces each one's most-neglected track. Works on day one.
        """
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            " trk AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                COALESCE(MAX(tp.c),0) plays "
            "         FROM tracks t LEFT JOIN tp ON tp.identity_key=t.identity_key "
            "         WHERE t.title<>'' AND t.artist<>'' GROUP BY t.identity_key), "
            " ap AS (SELECT artist, SUM(plays) total FROM trk GROUP BY artist), "
            " r AS (SELECT trk.*, ap.total atot, "
            "        ROW_NUMBER() OVER (PARTITION BY trk.artist ORDER BY trk.plays ASC, trk.key) rn "
            "       FROM trk JOIN ap ON ap.artist=trk.artist WHERE ap.total>0) "
            "SELECT key, title, artist, album, vid, thumb, plays, atot FROM r WHERE rn=1 "
            "ORDER BY atot DESC, plays ASC, key LIMIT :limit",
            {"limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "artist_plays": r["atot"]} for r in rows]

    @synchronized
    def complete_playlist(self, playlist_id, limit=20) -> list[dict]:
        """Tracks you own that fit a playlist but aren't in it.

        Fit = by an artist already in the playlist, and/or co-occurring with the playlist's
        tracks in your other playlists. Score weights same-artist above co-occurrence.
        """
        rows = self.conn.execute(
            "WITH pm AS (SELECT t.identity_key key, t.artist FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=:pid), "
            " pa AS (SELECT DISTINCT artist FROM pm WHERE artist<>''), "
            " shared AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id JOIN pm ON pm.key=t.identity_key "
            "            WHERE pt.playlist_id<>:pid), "
            " cand AS (SELECT t.identity_key key, MIN(t.title) title, MIN(t.artist) artist, "
            "                 MIN(t.album) album, MIN(t.video_id) vid, MIN(t.thumbnail) thumb, "
            "                 MAX(CASE WHEN t.artist IN (SELECT artist FROM pa) THEN 1 ELSE 0 END) sa, "
            "                 COUNT(DISTINCT CASE WHEN pt.playlist_id IN (SELECT pid FROM shared) "
            "                                THEN pt.playlist_id END) cooc "
            "          FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
            "          WHERE t.identity_key NOT IN (SELECT key FROM pm) AND t.title<>'' "
            "          GROUP BY t.identity_key) "
            "SELECT key, title, artist, album, vid, thumb, sa, cooc FROM cand "
            "WHERE sa=1 OR cooc>0 ORDER BY (sa*2+cooc) DESC, cooc DESC, key LIMIT :limit",
            {"pid": playlist_id, "limit": limit}).fetchall()
        return [{"key": r["key"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"],
                 "same_artist": bool(r["sa"]), "cooc": r["cooc"]} for r in rows]

    @synchronized
    def enrichment_candidates(self, limit=3, min_gaps=5, min_ratio=0.25) -> list[dict]:
        """Playlists worth enriching, ranked by how much you listen to them.

        Only playlists with a meaningful share of missing genre tags qualify (>= min_ratio of
        tracks, and at least min_gaps). This stops nagging about playlists you've already enriched
        down to a handful of untaggable residuals — what's left there isn't worth another pass,
        regardless of which providers ran. Enriching the most-played gappy playlists first gives
        the biggest recommendation lift, since recs lean on genre/year.
        """
        excl = self.excluded_playlist_ids()
        gen_where = (" AND p.id NOT IN (%s)" % ",".join(str(i) for i in excl)) if excl else ""
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key) "
            "SELECT p.id id, p.title title, p.thumbnail thumb, "
            "       SUM(CASE WHEN t.genre IS NULL OR t.genre='' THEN 1 ELSE 0 END) gaps, "
            "       COUNT(pt.track_id) total, COALESCE(SUM(tp.c),0) plays "
            "FROM playlists p JOIN playlist_tracks pt ON pt.playlist_id=p.id "
            "JOIN tracks t ON t.id=pt.track_id "
            "LEFT JOIN tp ON tp.identity_key=t.identity_key "
            f"WHERE 1=1{gen_where} "
            "GROUP BY p.id HAVING gaps >= :min_gaps AND (gaps * 1.0 / total) >= :min_ratio "
            "ORDER BY plays DESC, gaps DESC LIMIT :limit",
            {"limit": limit, "min_gaps": min_gaps, "min_ratio": min_ratio}).fetchall()
        return [{"id": r["id"], "title": r["title"], "thumbnail": r["thumb"], "gaps": r["gaps"],
                 "total": r["total"], "plays": r["plays"]} for r in rows]

    @synchronized
    def album_enrichment_candidates(self, limit=3, min_gaps=3, min_ratio=0.25) -> list[dict]:
        """Saved albums (folded into the library) with a meaningful share of missing genre tags —
        the album twin of enrichment_candidates. Enriching them sharpens recs, since the model now
        leans on these tracks too."""
        rows = self.conn.execute(
            "SELECT t.album_browse_id bid, MIN(sa.title) title, MIN(sa.thumbnail) thumb, "
            "       SUM(CASE WHEN t.genre IS NULL OR t.genre='' THEN 1 ELSE 0 END) gaps, COUNT(*) total "
            "FROM tracks t JOIN saved_albums sa ON sa.browse_id=t.album_browse_id "
            "WHERE t.album_browse_id IS NOT NULL "
            "GROUP BY t.album_browse_id HAVING gaps >= :min_gaps AND (gaps * 1.0 / total) >= :min_ratio "
            "ORDER BY gaps DESC LIMIT :limit",
            {"limit": limit, "min_gaps": min_gaps, "min_ratio": min_ratio}).fetchall()
        return [{"browse_id": r["bid"], "title": r["title"], "thumbnail": r["thumb"],
                 "gaps": r["gaps"], "total": r["total"]} for r in rows]
