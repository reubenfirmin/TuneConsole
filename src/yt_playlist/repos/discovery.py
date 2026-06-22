"""DiscoveryRepo — the background outward-discovery state: which artists we've scanned (and when),
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
  last_shown REAL                         -- last time it was surfaced on Home (anti-repeat)
);
CREATE TABLE IF NOT EXISTS discovered_artists (
  artist TEXT PRIMARY KEY,
  score REAL, because TEXT, fits TEXT, thumbnail TEXT,
  found_at REAL, last_shown REAL
);
"""

_GENERATED_PL = "(SELECT ytm FROM playlist_group WHERE name='Generated')"   # exclude app-made playlists


class DiscoveryRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        with self._lock:
            self.conn.executescript(_SCHEMA)

    # --- interest: every artist you engage with, ranked (plays + 2·playlists + 3·saved) ---
    @synchronized
    def interested_artists(self) -> list:
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
            "LEFT JOIN sv ON sv.a=arts.a ORDER BY score DESC").fetchall()
        return [{"artist": r["artist"], "score": r["score"]} for r in rows]

    @synchronized
    def artists_due_for_scan(self, now, ttl_days=5, budget=25) -> list:
        """Interested artists not scanned within ttl_days (never-scanned first, then oldest), capped
        at budget — exactly which artists the next worker pass should hit the network for."""
        cutoff = now - ttl_days * 86400
        scanned = {r["artist"]: r["scanned_at"] for r in
                   self.conn.execute("SELECT artist, scanned_at FROM artist_scans")}
        due = [a for a in self.interested_artists()
               if a["artist"] not in scanned or (scanned[a["artist"]] or 0.0) < cutoff]
        due.sort(key=lambda a: (scanned.get(a["artist"], float("-inf")), -a["score"]))  # never/oldest first
        return [a["artist"] for a in due[:budget]]

    @synchronized
    def mark_scanned(self, artist, now) -> None:
        self.conn.execute("INSERT INTO artist_scans(artist, scanned_at) VALUES (?,?) "
                          "ON CONFLICT(artist) DO UPDATE SET scanned_at=excluded.scanned_at", (artist, now))
        self.conn.commit()

    # --- the accumulating pools ---
    @synchronized
    def upsert_discovered_album(self, browse_id, artist, title, year, thumbnail, now) -> None:
        """Add an album to the pool (first-seen found_at is preserved on re-discovery)."""
        self.conn.execute(
            "INSERT INTO discovered_albums(browse_id, artist, title, year, thumbnail, found_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(browse_id) DO UPDATE SET "
            "artist=excluded.artist, title=excluded.title, year=excluded.year, thumbnail=excluded.thumbnail",
            (browse_id, artist, title, year, thumbnail, now))
        self.conn.commit()

    @synchronized
    def upsert_discovered_artist(self, artist, score, because, fits, thumbnail, now) -> None:
        self.conn.execute(
            "INSERT INTO discovered_artists(artist, score, because, fits, thumbnail, found_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(artist) DO UPDATE SET score=excluded.score, because=excluded.because, "
            "fits=excluded.fits, thumbnail=excluded.thumbnail",
            (artist, score, json.dumps(because or []), json.dumps(fits or []), thumbnail, now))
        self.conn.commit()

    @synchronized
    def get_discovered_albums(self) -> list:
        return [{"browse_id": r["browse_id"], "artist": r["artist"], "title": r["title"],
                 "year": r["year"], "thumbnail": r["thumbnail"], "found_at": r["found_at"],
                 "last_shown": r["last_shown"]}
                for r in self.conn.execute("SELECT * FROM discovered_albums")]

    @synchronized
    def get_discovered_artists(self) -> list:
        return [{"artist": r["artist"], "score": r["score"], "because": json.loads(r["because"] or "[]"),
                 "fits": json.loads(r["fits"] or "[]"), "thumbnail": r["thumbnail"],
                 "found_at": r["found_at"], "last_shown": r["last_shown"]}
                for r in self.conn.execute("SELECT * FROM discovered_artists")]

    @synchronized
    def mark_shown(self, kind, ids, now) -> None:
        """Stamp last_shown for what we just surfaced. kind='album' (browse_ids) | 'artist' (names)."""
        tbl, col = ("discovered_albums", "browse_id") if kind == "album" else ("discovered_artists", "artist")
        self.conn.executemany(f"UPDATE {tbl} SET last_shown=? WHERE {col}=?", [(now, i) for i in ids])
        self.conn.commit()

    @synchronized
    def prune_discovered(self, owned_albums, saved_browse_ids, owned_artists) -> None:
        """Drop pool entries you've since acquired: albums you now own/saved, artists now in library."""
        for r in self.conn.execute("SELECT browse_id, title FROM discovered_albums").fetchall():
            if r["browse_id"] in saved_browse_ids or (r["title"] or "").lower() in owned_albums:
                self.conn.execute("DELETE FROM discovered_albums WHERE browse_id=?", (r["browse_id"],))
        if owned_artists:
            from yt_playlist.matching import normalize
            for r in self.conn.execute("SELECT artist FROM discovered_artists").fetchall():
                if normalize(r["artist"]) in owned_artists:
                    self.conn.execute("DELETE FROM discovered_artists WHERE artist=?", (r["artist"],))
        self.conn.commit()
