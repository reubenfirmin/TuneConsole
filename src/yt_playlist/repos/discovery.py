"""DiscoveryRepo: the background outward-discovery state: which artists we've scanned (and when),
and the accumulating pools of new albums / new artists we've found, with last-shown bookkeeping.

Owns its own tables (created lazily/idempotently). The interest signal (which artists are worth
scanning) is computed live from your library: play counts, playlist participation, saved albums.
"""
import json

from yt_playlist.repos.base import Repo, synchronized

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_scans (
  artist TEXT PRIMARY KEY,
  scanned_at REAL                         -- last time we hit YT/Last.fm for this artist
);
CREATE TABLE IF NOT EXISTS discovered_albums (
  browse_id TEXT PRIMARY KEY,
  artist TEXT, title TEXT, year TEXT, thumbnail TEXT,
  found_at REAL,                          -- first time we discovered it (drives recency)
  last_shown REAL,                        -- last time it was surfaced on Home (anti-repeat)
  genre TEXT                              -- #18: candidate genre for the facet overlay
);
CREATE TABLE IF NOT EXISTS discovered_artists (
  artist TEXT PRIMARY KEY,
  score REAL, because TEXT, fits TEXT, thumbnail TEXT,
  found_at REAL, last_shown REAL,
  genre TEXT                              -- #18: candidate genre for the facet overlay
);
CREATE TABLE IF NOT EXISTS discovered_tracks (
  identity_key TEXT PRIMARY KEY,          -- #13 Phase 2: out-of-corpus candidate tracks for clusters
  video_id TEXT, title TEXT, artist TEXT, album TEXT, thumbnail TEXT,
  genre TEXT, year TEXT,
  bpm REAL, energy REAL, danceability REAL, mood_happy REAL, mood_sad REAL,
  mood_relaxed REAL, mood_acoustic REAL, instrumental REAL, loudness REAL,
  dynamic_complexity REAL, music_key TEXT, music_scale TEXT,
  source_browse_id TEXT,                  -- the discovered album/artist it came from
  found_at REAL,
  last_enriched REAL                      -- #50: when the cold-enrichment waterfall last probed it (NULL = queued)
);
"""

# Audio/content columns persisted per discovered track (mirrors the library `tracks` audio set).
_DTRACK_AUDIO = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                 "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                 "music_key", "music_scale")

_GENERATED_PL = "(SELECT ytm FROM playlist_group WHERE name='Generated')"   # exclude app-made playlists


class DiscoveryRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        with self._lock:
            self.conn.executescript(_SCHEMA)
            for tbl in ("discovered_albums", "discovered_artists"):   # #18: add genre to pre-existing DBs
                cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({tbl})")}
                if "genre" not in cols:
                    self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN genre TEXT")
            dt_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(discovered_tracks)")}
            if "last_enriched" not in dt_cols:                        # #50: cold-enrichment queue stamp
                self.conn.execute("ALTER TABLE discovered_tracks ADD COLUMN last_enriched REAL")
            # #52/#53: engagement tracking on every pool (first_shown = GC clock start; offered_count =
            # times surfaced; last_shown back-filled onto discovered_tracks, which lacked it).
            for tbl in ("discovered_albums", "discovered_artists", "discovered_tracks"):
                cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({tbl})")}
                for col, decl in (("first_shown", "REAL"), ("last_shown", "REAL"),
                                  ("offered_count", "INTEGER NOT NULL DEFAULT 0")):
                    if col not in cols:
                        self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
            self.conn.commit()

    # --- interest: every artist you engage with, ranked by engagement intensity. Weights ramp with how
    # deliberate the act is: a play is passive (1), filing into a playlist is active curation (2), saving
    # a whole album is the strongest endorsement (3). Tune the multipliers in the SELECT below. ---
    @synchronized
    def interested_artists(self, limit=None) -> list:
        """Engaged artists, highest interest first. `limit` (#52) keeps only the top-N so discovery
        scans a focused set instead of every artist you have ever touched."""
        rows = self.conn.execute(
            "WITH plays AS (SELECT t.artist a, COUNT(*) n FROM history_items hi "
            "                 JOIN tracks t ON t.identity_key=hi.identity_key "
            "                 WHERE t.artist<>'' GROUP BY t.artist), "
            "     pls AS (SELECT t.artist a, COUNT(DISTINCT pt.playlist_id) n "
            "               FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "               JOIN playlists p ON p.id=pt.playlist_id "
            f"              WHERE t.artist<>'' AND p.ytm_playlist_id NOT IN {_GENERATED_PL} "
            "               GROUP BY t.artist), "
            "     sv AS (SELECT artist a, COUNT(*) n FROM saved_albums WHERE artist<>'' GROUP BY artist), "
            "     arts AS (SELECT a FROM plays UNION SELECT a FROM pls UNION SELECT a FROM sv) "
            "SELECT arts.a artist, "
            "       COALESCE(plays.n,0)*1.0 + COALESCE(pls.n,0)*2.0 + COALESCE(sv.n,0)*3.0 score "
            "FROM arts LEFT JOIN plays ON plays.a=arts.a LEFT JOIN pls ON pls.a=arts.a "
            "LEFT JOIN sv ON sv.a=arts.a ORDER BY score DESC"
            + (" LIMIT ?" if limit is not None else ""),
            (limit,) if limit is not None else ()).fetchall()
        return [{"artist": r["artist"], "score": r["score"]} for r in rows]

    @synchronized
    def artists_due_for_scan(self, now, ttl_days=5, budget=25, artist_limit=None) -> list:
        """Interested artists not scanned within ttl_days (never-scanned first, then oldest), capped
        at budget, exactly which artists the next worker pass should hit the network for. `artist_limit`
        (#52) restricts the universe to your top-N most-engaged artists, so discovery stays focused.

        ttl_days=5 re-scans an artist about weekly (their catalogue changes slowly); budget=25 rate-
        limits each pass so we don't burst the Last.fm/MusicBrainz APIs."""
        cutoff = now - ttl_days * 86400
        scanned = {r["artist"]: r["scanned_at"] for r in
                   self.conn.execute("SELECT artist, scanned_at FROM artist_scans")}
        due = [a for a in self.interested_artists(limit=artist_limit)
               if a["artist"] not in scanned or (scanned[a["artist"]] or 0.0) < cutoff]
        due.sort(key=lambda a: (scanned.get(a["artist"], float("-inf")), -a["score"]))  # never/oldest first
        return [a["artist"] for a in due[:budget]]

    @synchronized
    def discovered_albums_for_artist(self, artist) -> dict:
        """{browse_id: offered_count} for one artist's pooled albums (#52 rotation input)."""
        return {r["browse_id"]: (r["offered_count"] or 0) for r in self.conn.execute(
            "SELECT browse_id, offered_count FROM discovered_albums WHERE artist=?", (artist,))}

    @synchronized
    def delete_discovered_albums(self, browse_ids) -> int:
        """Delete the given pooled albums (rotation / bounds enforcement). Returns the count removed."""
        ids = list(browse_ids)
        if not ids:
            return 0
        self.conn.executemany("DELETE FROM discovered_albums WHERE browse_id=?", [(b,) for b in ids])
        self.conn.commit()
        return len(ids)

    @synchronized
    def prune_orphan_discovered_tracks(self) -> int:
        """Drop discovered tracks whose source album is no longer in the pool (#52 cleanup). Radio-
        sourced tracks (source_browse_id 'radio:*') have their own lifecycle and are kept. Returns the
        count removed."""
        cur = self.conn.execute(
            "DELETE FROM discovered_tracks WHERE source_browse_id NOT LIKE 'radio:%' "
            "AND source_browse_id NOT IN (SELECT browse_id FROM discovered_albums)")
        self.conn.commit()
        return cur.rowcount

    @synchronized
    def mark_scanned(self, artist, now) -> None:
        self.conn.execute("INSERT INTO artist_scans(artist, scanned_at) VALUES (?,?) "
                          "ON CONFLICT(artist) DO UPDATE SET scanned_at=excluded.scanned_at", (artist, now))
        self.conn.commit()

    # --- the accumulating pools ---
    @synchronized
    def upsert_discovered_album(self, browse_id, artist, title, year, thumbnail, now, genre=None) -> None:
        """Add an album to the pool (first-seen found_at is preserved on re-discovery). `genre` is the
        candidate genre for the #18 facet overlay; a later re-discovery with a genre fills it in."""
        self.conn.execute(
            "INSERT INTO discovered_albums(browse_id, artist, title, year, thumbnail, found_at, genre) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(browse_id) DO UPDATE SET "
            "artist=excluded.artist, title=excluded.title, year=excluded.year, "
            "thumbnail=excluded.thumbnail, genre=COALESCE(excluded.genre, discovered_albums.genre)",
            (browse_id, artist, title, year, thumbnail, now, genre))
        self.conn.commit()

    @synchronized
    def upsert_discovered_artist(self, artist, score, because, fits, thumbnail, now, genre=None) -> None:
        self.conn.execute(
            "INSERT INTO discovered_artists(artist, score, because, fits, thumbnail, found_at, genre) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(artist) DO UPDATE SET score=excluded.score, "
            "because=excluded.because, fits=excluded.fits, thumbnail=excluded.thumbnail, "
            "genre=COALESCE(excluded.genre, discovered_artists.genre)",
            (artist, score, json.dumps(because or []), json.dumps(fits or []), thumbnail, now, genre))
        self.conn.commit()

    @synchronized
    def get_discovered_albums(self) -> list:
        return [{"browse_id": r["browse_id"], "artist": r["artist"], "title": r["title"],
                 "year": r["year"], "thumbnail": r["thumbnail"], "found_at": r["found_at"],
                 "last_shown": r["last_shown"], "genre": r["genre"],
                 "first_shown": r["first_shown"], "offered_count": r["offered_count"]}
                for r in self.conn.execute("SELECT * FROM discovered_albums")]

    @synchronized
    def get_discovered_artists(self) -> list:
        return [{"artist": r["artist"], "score": r["score"], "because": json.loads(r["because"] or "[]"),
                 "fits": json.loads(r["fits"] or "[]"), "thumbnail": r["thumbnail"],
                 "found_at": r["found_at"], "last_shown": r["last_shown"], "genre": r["genre"],
                 "first_shown": r["first_shown"], "offered_count": r["offered_count"]}
                for r in self.conn.execute("SELECT * FROM discovered_artists")]

    # --- #13 Phase 2: out-of-corpus candidate tracks (for the Clusters "reach for new music" mode) ---
    @synchronized
    def upsert_discovered_track(self, identity_key, video_id, title, artist, album, thumbnail,
                                genre, year, source_browse_id, now, audio=None) -> None:
        audio = audio or {}
        cols = (["identity_key", "video_id", "title", "artist", "album", "thumbnail", "genre", "year",
                 "source_browse_id", "found_at"] + list(_DTRACK_AUDIO))
        vals = ([identity_key, video_id, title, artist, album, thumbnail, genre, year,
                 source_browse_id, now] + [audio.get(c) for c in _DTRACK_AUDIO])
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("identity_key", "found_at"))
        self.conn.execute(
            f"INSERT INTO discovered_tracks({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(identity_key) DO UPDATE SET {updates}", vals)
        self.conn.commit()

    @staticmethod
    def _dtrack_row(r) -> dict:
        d = {k: r[k] for k in ("identity_key", "video_id", "title", "artist", "album", "thumbnail",
                               "genre", "year", "source_browse_id", "found_at",
                               "first_shown", "last_shown", "offered_count")}
        d["audio"] = {c: r[c] for c in _DTRACK_AUDIO if r[c] is not None}
        return d

    @synchronized
    def mark_offered(self, kind, ids, now) -> None:
        """#52/#53: stamp a surfacing of pool items. Sets last_shown, sets first_shown once (the GC
        clock start), and bumps offered_count. kind='album'(browse_id) | 'artist'(name) |
        'track'(identity_key). Only existing rows are touched (library keys passed in are no-ops)."""
        tbl, col = {"album": ("discovered_albums", "browse_id"),
                    "artist": ("discovered_artists", "artist"),
                    "track": ("discovered_tracks", "identity_key")}[kind]
        self.conn.executemany(
            f"UPDATE {tbl} SET last_shown=?, first_shown=COALESCE(first_shown, ?), "
            f"offered_count=offered_count+1 WHERE {col}=?", [(now, now, i) for i in ids])
        self.conn.commit()

    @synchronized
    def get_discovered_tracks(self) -> list:
        return [self._dtrack_row(r) for r in self.conn.execute("SELECT * FROM discovered_tracks")]

    @synchronized
    def discovered_tracks_by_keys(self, keys) -> dict:
        keys = list(keys)
        if not keys:
            return {}
        qs = ",".join("?" * len(keys))
        return {r["identity_key"]: self._dtrack_row(r) for r in self.conn.execute(
            f"SELECT * FROM discovered_tracks WHERE identity_key IN ({qs})", keys)}

    # --- #50 cold-enrichment queue: enrich the discovered pool (genre + audio) so the cold ranker can
    #     score it and the audio tilt fires. Separate from the library next_enrich_batch queue. ---
    @synchronized
    def next_discovered_enrich_batch(self, limit) -> list:
        """Up to `limit` not-yet-probed discovered tracks, newest pull first (found_at DESC) so freshly
        pulled candidates jump the queue. v1 probes each row once (last_enriched IS NULL gate)."""
        rows = self.conn.execute(
            "SELECT identity_key, video_id, title, artist FROM discovered_tracks "
            "WHERE last_enriched IS NULL ORDER BY found_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def set_discovered_enrichment(self, identity_key, genre, year) -> None:
        """Fill-only genre/year on a discovered track (a None argument leaves the existing value)."""
        self.conn.execute(
            "UPDATE discovered_tracks SET genre=COALESCE(?, genre), year=COALESCE(?, year) "
            "WHERE identity_key=?", (genre, year, identity_key))
        self.conn.commit()

    @synchronized
    def set_discovered_audio(self, identity_key, **audio) -> None:
        """Write the audio columns this table holds; ignore any extra provider fields (e.g. popularity,
        gain, label) that have no discovered_tracks column."""
        cols = [c for c in _DTRACK_AUDIO if c in audio]
        if not cols:
            return
        sets = ", ".join(f"{c}=?" for c in cols)
        self.conn.execute(f"UPDATE discovered_tracks SET {sets} WHERE identity_key=?",
                          [audio[c] for c in cols] + [identity_key])
        self.conn.commit()

    @synchronized
    def mark_discovered_enriched(self, identity_keys, now) -> None:
        """Stamp last_enriched so a probed row leaves the queue (probe-once for v1)."""
        keys = list(identity_keys)
        if not keys:
            return
        qs = ",".join("?" * len(keys))
        self.conn.execute(f"UPDATE discovered_tracks SET last_enriched=? WHERE identity_key IN ({qs})",
                          [now] + keys)
        self.conn.commit()

    @synchronized
    def prune_discovered_tracks(self, library_keys, held_keys=()) -> None:
        """Drop candidate tracks you've since acquired (now in the library), EXCEPT keys in held_keys.
        #52: a track that is only in a live generated playlist is held in the pool until that playlist
        is GC'd or promoted, so its synced copy must not prune it as 'acquired' in the meantime."""
        held = set(held_keys or ())
        rows = self.conn.execute("SELECT identity_key FROM discovered_tracks").fetchall()
        gone = [r["identity_key"] for r in rows
                if r["identity_key"] in library_keys and r["identity_key"] not in held]
        if gone:
            self.conn.executemany("DELETE FROM discovered_tracks WHERE identity_key=?",
                                  [(k,) for k in gone])
            self.conn.commit()

    # --- #53 Tools viewer: per-pool rows with engagement stats and the projected GC date ---
    @staticmethod
    def _gc_at(first_shown, gc_days):
        return (first_shown + gc_days * 86400) if first_shown is not None else None

    @synchronized
    def discovery_track_view(self, now, gc_days) -> list:
        """Discovered tracks with play count (history), library-playlist membership count, offered
        count, and projected GC date (first_shown + gc_days, or None when never shown). Newest first."""
        rows = self.conn.execute(
            "SELECT dt.*, "
            "  (SELECT COUNT(*) FROM history_items hi WHERE hi.identity_key=dt.identity_key) plays, "
            "  (SELECT COUNT(DISTINCT pt.playlist_id) FROM tracks t "
            "     JOIN playlist_tracks pt ON pt.track_id=t.id WHERE t.identity_key=dt.identity_key) playlists "
            "FROM discovered_tracks dt ORDER BY dt.found_at DESC").fetchall()
        out = []
        for r in rows:
            d = self._dtrack_row(r)
            d.update(plays=r["plays"], playlists=r["playlists"],
                     gc_at=self._gc_at(r["first_shown"], gc_days))
            out.append(d)
        return out

    @synchronized
    def discovery_album_view(self, now, gc_days) -> list:
        rows = self.conn.execute("SELECT * FROM discovered_albums ORDER BY found_at DESC").fetchall()
        return [{**{k: r[k] for k in ("browse_id", "artist", "title", "year", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count")},
                 "gc_at": self._gc_at(r["first_shown"], gc_days)} for r in rows]

    @synchronized
    def discovery_artist_view(self, now, gc_days) -> list:
        rows = self.conn.execute("SELECT * FROM discovered_artists ORDER BY found_at DESC").fetchall()
        return [{**{k: r[k] for k in ("artist", "score", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count")},
                 "gc_at": self._gc_at(r["first_shown"], gc_days)} for r in rows]

    @synchronized
    def gc_discovery_pool(self, now, gc_days, held_track_keys) -> dict:
        """#52: strict GC of pool items first shown longer than gc_days ago and never added. Tracks in
        held_track_keys (live in a not-yet-GC'd generated playlist) are kept. Items never shown
        (first_shown NULL) are not touched. The acquisition prune runs separately. Returns delete counts."""
        cutoff = now - gc_days * 86400
        out = {}
        for key, tbl in (("albums", "discovered_albums"), ("artists", "discovered_artists")):
            cur = self.conn.execute(
                f"DELETE FROM {tbl} WHERE first_shown IS NOT NULL AND first_shown < ?", (cutoff,))
            out[key] = cur.rowcount
        held = set(held_track_keys or ())
        rows = self.conn.execute(
            "SELECT identity_key FROM discovered_tracks WHERE first_shown IS NOT NULL AND first_shown < ?",
            (cutoff,)).fetchall()
        gone = [r["identity_key"] for r in rows if r["identity_key"] not in held]
        if gone:
            self.conn.executemany("DELETE FROM discovered_tracks WHERE identity_key=?", [(k,) for k in gone])
        out["tracks"] = len(gone)
        self.conn.commit()
        return out

    def mark_shown(self, kind, ids, now) -> None:
        """Back-compat alias for mark_offered: stamp a surfacing (last_shown + first_shown + offered_count).
        kind='album' (browse_ids) | 'artist' (names)."""
        self.mark_offered(kind, ids, now)

    @synchronized
    def prune_discovered(self, owned_albums, saved_browse_ids, owned_artists) -> None:
        """Drop pool entries you've since acquired: albums you now own/saved, artists now in library."""
        for r in self.conn.execute("SELECT browse_id, title FROM discovered_albums").fetchall():
            if r["browse_id"] in saved_browse_ids or (r["title"] or "").lower() in owned_albums:
                self.conn.execute("DELETE FROM discovered_albums WHERE browse_id=?", (r["browse_id"],))
        if owned_artists:
            from yt_playlist.util.matching import normalize
            for r in self.conn.execute("SELECT artist FROM discovered_artists").fetchall():
                if normalize(r["artist"]) in owned_artists:
                    self.conn.execute("DELETE FROM discovered_artists WHERE artist=?", (r["artist"],))
        self.conn.commit()
