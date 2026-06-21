"""RecRepo — recommendation persistence (impressions, materialized proposals, taste queries).

Unified onto the shared Repo base: binds the Store's connection + re-entrant lock and serializes
with @synchronized like every other DAO. It owns its own rec tables (created lazily, idempotently)
so new rec persistence lives here rather than growing store.py.
"""
import json

from yt_playlist.repos.base import Repo, synchronized

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rec_impressions (
  surface TEXT NOT NULL,                  -- 'for_you' | 'explore' | 'suggest'
  item_key TEXT NOT NULL,
  views INTEGER NOT NULL DEFAULT 0,       -- times shown (debounced), for anti-staleness erosion
  last_shown REAL,                        -- drives the recycle cooldown
  PRIMARY KEY (surface, item_key)
);
CREATE TABLE IF NOT EXISTS rec_proposals (
  surface TEXT PRIMARY KEY,               -- 'auto_playlists' | 'discover'
  payload TEXT NOT NULL,                  -- JSON, materialized by the rec worker (last-good serving)
  built_at REAL
);
CREATE TABLE IF NOT EXISTS rec_artist_similar (
  artist TEXT PRIMARY KEY,                -- anchor artist (display name)
  payload TEXT NOT NULL,                  -- JSON [[name, match], ...] from Last.fm
  fetched_at REAL
);
"""


class RecRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        with self._lock:
            self.conn.executescript(_SCHEMA)   # this DAO owns its tables (idempotent)

    @synchronized
    def record_impressions(self, surface, keys, now, debounce_s=300) -> None:
        """Count that these items were shown. Debounced: a re-show within debounce_s doesn't
        re-count, so htmx lazy-load / polling don't inflate the view count."""
        for k in keys:
            row = self.conn.execute(
                "SELECT views, last_shown FROM rec_impressions WHERE surface=? AND item_key=?",
                (surface, k)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO rec_impressions(surface,item_key,views,last_shown) VALUES (?,?,1,?)",
                    (surface, k, now))
            elif row["last_shown"] is None or now - row["last_shown"] >= debounce_s:
                self.conn.execute(
                    "UPDATE rec_impressions SET views=views+1, last_shown=? "
                    "WHERE surface=? AND item_key=?", (now, surface, k))
        self.conn.commit()

    @synchronized
    def eroded_keys(self, surface, now, view_cap=3, cooldown_days=14) -> set:
        """Items shown >= view_cap times whose cooldown hasn't elapsed — hide them to keep the
        surface fresh, then recycle once the cooldown passes."""
        cutoff = now - cooldown_days * 86400
        return {r["item_key"] for r in self.conn.execute(
            "SELECT item_key FROM rec_impressions WHERE surface=? AND views>=? AND last_shown>?",
            (surface, view_cap, cutoff))}

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
    def tracks_total(self) -> int:
        """Total tracks in the library (taste-model coverage denominator)."""
        return self.conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]

    @synchronized
    def put_proposals(self, surface, data, now=None) -> None:
        """Materialize a surface's proposals as JSON (the rec worker writes; routes read last-good)."""
        self.conn.execute(
            "INSERT INTO rec_proposals(surface, payload, built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload, built_at=excluded.built_at",
            (surface, json.dumps(data), now))
        self.conn.commit()

    @synchronized
    def get_proposals(self, surface):
        """Last materialized proposals for a surface, or None if never built."""
        row = self.conn.execute(
            "SELECT payload FROM rec_proposals WHERE surface=?", (surface,)).fetchone()
        return json.loads(row["payload"]) if row else None

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
    def cached_similar(self, artist, now, ttl_days=14):
        """Cached Last.fm similar-artist list, or None if missing/expired."""
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM rec_artist_similar WHERE artist=?", (artist,)).fetchone()
        if row is None or row["fetched_at"] is None or now - row["fetched_at"] > ttl_days * 86400:
            return None
        return json.loads(row["payload"])

    @synchronized
    def cache_similar(self, artist, pairs, now) -> None:
        self.conn.execute(
            "INSERT INTO rec_artist_similar(artist, payload, fetched_at) VALUES (?,?,?) "
            "ON CONFLICT(artist) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (artist, json.dumps(pairs), now))
        self.conn.commit()

    @synchronized
    def genre_play_distribution(self) -> dict:
        """{genre: Σ (1 + play_count)} over tagged tracks — play-weighted so a barely-played
        context counts toward breadth/palette far less than one you actually listen to (the +1 keeps
        owned-but-unplayed tracks from vanishing entirely)."""
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key) "
            "SELECT t.genre g, SUM(1 + COALESCE(tp.c, 0)) w FROM tracks t "
            "LEFT JOIN tp ON tp.identity_key = t.identity_key "
            "WHERE t.genre <> '' GROUP BY t.genre").fetchall()
        return {r["g"]: r["w"] for r in rows}

    @synchronized
    def saved_album_ids(self) -> set:
        return {r["browse_id"] for r in self.conn.execute(
            "SELECT browse_id FROM saved_albums")}

    @synchronized
    def feedback_summary(self) -> dict:
        """{kind: count} of stored feedback events — for the Taste Model page."""
        return {r["kind"]: r["c"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) c FROM rec_feedback GROUP BY kind")}

    @synchronized
    def clear_feedback(self) -> None:
        self.conn.execute("DELETE FROM rec_feedback")
        self.conn.execute("DELETE FROM rec_impressions")
        self.conn.commit()

    # --- learned taste model: blend weights, feedback, embedding vectors, training inputs.
    #     (rec_weights / rec_feedback / rec_vectors tables live in store.py's central SCHEMA.) ---
    @synchronized
    def genre_distribution(self) -> dict:
        """{genre: track_count} over tagged tracks — feeds the taste-breadth/palette computation."""
        return {r["genre"]: r["c"] for r in self.conn.execute(
            "SELECT genre, COUNT(*) c FROM tracks WHERE genre<>'' GROUP BY genre")}

    @synchronized
    def get_weights(self) -> dict:
        """Learned blend weights by axis (missing axis = prior 1.0)."""
        return {r["axis"]: r["weight"] for r in self.conn.execute("SELECT axis, weight FROM rec_weights")}

    @synchronized
    def nudge_weight(self, axis, factor, lo=0.2, hi=3.0) -> float:
        """Multiply an axis weight by factor (clamped), then shrink slightly toward the 1.0 prior."""
        row = self.conn.execute("SELECT weight FROM rec_weights WHERE axis=?", (axis,)).fetchone()
        w = max(lo, min(hi, (row["weight"] if row else 1.0) * factor))
        w = w + (1.0 - w) * 0.05
        self.conn.execute("INSERT INTO rec_weights(axis, weight) VALUES (?, ?) "
                          "ON CONFLICT(axis) DO UPDATE SET weight=excluded.weight", (axis, w))
        self.conn.commit()
        return w

    @synchronized
    def set_weight(self, axis, weight) -> None:
        """Manual override (Taste Model page)."""
        self.conn.execute("INSERT INTO rec_weights(axis, weight) VALUES (?, ?) "
                          "ON CONFLICT(axis) DO UPDATE SET weight=excluded.weight", (axis, float(weight)))
        self.conn.commit()

    @synchronized
    def reset_weights(self) -> None:
        self.conn.execute("DELETE FROM rec_weights")
        self.conn.commit()

    @synchronized
    def record_feedback(self, surface, item_key, kind, reason=None, scope="", until=None, now=None) -> None:
        """Persist a feedback event (dismiss/less/more/mute/not_now). Upserts per (surface,item,scope)."""
        self.conn.execute(
            "INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET "
            "kind=excluded.kind, reason=excluded.reason, until=excluded.until, created_at=excluded.created_at",
            (surface, item_key, kind, reason, scope or "", until, now))
        self.conn.commit()

    @synchronized
    def suppressed_keys(self, surface, now, scope="") -> set:
        """Track keys to hide on a surface: dismissed/muted/snoozed, honoring any 'until' expiry."""
        rows = self.conn.execute(
            "SELECT item_key FROM rec_feedback WHERE surface=? AND (scope='' OR scope=?) "
            "AND kind IN ('dismiss','mute','not_now') AND (until IS NULL OR until>?)",
            (surface, scope or "", now)).fetchall()
        return {r["item_key"] for r in rows}

    @synchronized
    def muted_artists(self) -> set:
        """Artist names the user has muted (stored as item_key 'artist:<name>')."""
        rows = self.conn.execute("SELECT item_key FROM rec_feedback WHERE kind='mute'").fetchall()
        return {r["item_key"][7:] for r in rows if r["item_key"].startswith("artist:")}

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
        out = []
        # structural baskets: tracks grouped by a shared column
        for grp, cap in (
            ("SELECT pt.playlist_id g, t.identity_key k FROM playlist_tracks pt "
             "JOIN tracks t ON t.id=pt.track_id", max_playlist),
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

    @synchronized
    def replace_rec_vectors(self, rows) -> None:
        """Atomically replace all taste-embedding vectors. rows = iterable of (identity_key, bytes)."""
        self.conn.execute("DELETE FROM rec_vectors")
        self.conn.executemany("INSERT INTO rec_vectors(identity_key, vec) VALUES (?,?)", rows)
        self.conn.commit()

    @synchronized
    def get_rec_vectors(self) -> list[tuple]:
        return [(r["identity_key"], r["vec"])
                for r in self.conn.execute("SELECT identity_key, vec FROM rec_vectors")]

    @synchronized
    def rec_vectors_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM rec_vectors").fetchone()["c"]

    # --- candidate generators + lookups for the recommendation surfaces (read-only over the library) ---
    @synchronized
    def resurface_candidates(self, now, window_days=90, min_plays=2, limit=50) -> list[dict]:
        """'Forgotten gems': songs played often overall but not within the recent window.

        play count = appearances across history snapshots; last_played = newest snapshot
        containing the song. Returns songs with >= min_plays whose last play predates the
        window (now - window_days), most-played and longest-unplayed first.
        """
        cutoff = now - window_days * 86400.0
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key, COUNT(*) c, MAX(hs.taken_at) last "
            "  FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            "  GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(title) title, MIN(artist) artist, "
            "               MIN(album) album, MIN(video_id) vid, MIN(thumbnail) thumb "
            "               FROM tracks GROUP BY identity_key) "
            "SELECT n.identity_key k, n.title, n.artist, n.album, n.vid, n.thumb, p.c plays, p.last last "
            "FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "WHERE n.title <> '' AND p.c >= :min_plays AND p.last < :cutoff "
            "ORDER BY p.c DESC, p.last ASC LIMIT :limit",
            {"min_plays": min_plays, "cutoff": cutoff, "limit": limit}).fetchall()
        return [{"key": r["k"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
                 "video_id": r["vid"], "thumbnail": r["thumb"], "plays": r["plays"],
                 "last_played": r["last"]} for r in rows]

    @synchronized
    def more_like_rotation(self, seed_limit=40, limit=40) -> list[dict]:
        """Tracks that share a playlist with your most-played songs but that you barely play.

        Collaborative signal: 'because you listen to X, and these live alongside X in your
        playlists.' Seeds = your top-played songs; candidates = co-members of their playlists.
        """
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key), "
            " seeds AS (SELECT identity_key k FROM tp ORDER BY c DESC LIMIT :seed_limit), "
            " seedpl AS (SELECT DISTINCT pt.playlist_id pid FROM playlist_tracks pt "
            "            JOIN tracks t ON t.id=pt.track_id JOIN seeds s ON s.k=t.identity_key), "
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
        rows = self.conn.execute(
            "WITH tp AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key) "
            "SELECT p.id id, p.title title, p.thumbnail thumb, "
            "       SUM(CASE WHEN t.genre IS NULL OR t.genre='' THEN 1 ELSE 0 END) gaps, "
            "       COUNT(pt.track_id) total, COALESCE(SUM(tp.c),0) plays "
            "FROM playlists p JOIN playlist_tracks pt ON pt.playlist_id=p.id "
            "JOIN tracks t ON t.id=pt.track_id "
            "LEFT JOIN tp ON tp.identity_key=t.identity_key "
            "GROUP BY p.id HAVING gaps >= :min_gaps AND (gaps * 1.0 / total) >= :min_ratio "
            "ORDER BY plays DESC, gaps DESC LIMIT :limit",
            {"limit": limit, "min_gaps": min_gaps, "min_ratio": min_ratio}).fetchall()
        return [{"id": r["id"], "title": r["title"], "thumbnail": r["thumb"], "gaps": r["gaps"],
                 "total": r["total"], "plays": r["plays"]} for r in rows]

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
    def genre_cooccurrence(self) -> dict:
        """How often each unordered genre pair shares a playlist — the corpus adjacency signal.

        Returns {"pairs": {(g1,g2): count}, "occ": {genre: #playlists}}. Used to pull genres the
        user repeatedly playlists together closer than the static map alone (spec §2.1/§5.3).
        """
        from collections import Counter
        pl = {}
        for r in self.conn.execute(
            "SELECT pt.playlist_id pid, t.genre g FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id WHERE t.genre<>''"):
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

    @synchronized
    def top_played_keys(self, limit=10) -> list[str]:
        """Identity keys of your most-played songs (for seeding taste-neighbourhood recs)."""
        rows = self.conn.execute(
            "SELECT identity_key k, COUNT(*) c FROM history_items GROUP BY identity_key "
            "ORDER BY c DESC LIMIT ?", (limit,)).fetchall()
        return [r["k"] for r in rows]
