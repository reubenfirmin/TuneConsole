"""DiscoveryRepo: the background outward-discovery state: which artists we've scanned (and when),
and the accumulating pools of new albums / new artists we've found, with last-shown bookkeeping.

Tables are created by numbered migrations. The interest signal (which artists are worth
scanning) is computed live from your library: play counts, playlist participation, saved albums.
"""
import json

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.core import catalog

# Audio/content columns persisted per discovered track (mirrors the library `tracks` audio set).
_DTRACK_AUDIO = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                 "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                 "music_key", "music_scale")

_GENERATED_PL = "(SELECT ytm FROM playlist_group WHERE name='Generated')"   # exclude app-made playlists


class DiscoveryRepo(Repo):

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
    def prune_orphan_discovered_tracks(self, held_keys=()) -> int:
        """Drop discovered tracks whose source album is no longer in the pool (#52 cleanup). Radio-
        and genre-sourced tracks have their own lifecycle and are kept. Returns the
        count removed."""
        rows = self.conn.execute(
            "SELECT r.identity_key FROM discovery_memberships d JOIN recordings r ON r.id=d.recording_id "
            "WHERE EXISTS(SELECT 1 FROM discovery_sources s WHERE s.recording_id=d.recording_id) "
            "AND NOT EXISTS(SELECT 1 FROM discovery_sources s WHERE s.recording_id=d.recording_id "
            "AND (s.source_token LIKE 'radio:%' OR s.source_token LIKE 'genre:%' "
            "OR s.source_token IN (SELECT browse_id FROM discovered_albums)))").fetchall()
        held = set(held_keys)
        gone = [(r["identity_key"],) for r in rows if r["identity_key"] not in held]
        self.conn.executemany("DELETE FROM discovered_tracks WHERE identity_key=?", gone)
        self.conn.commit()
        return len(gone)

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
                                genre, year, source_browse_id, now, audio=None) -> str:
        with self.conn:
            entry_id = catalog.upsert(self.conn, video_id, title, artist, album,
                                      thumbnail=thumbnail, created_at=now, library=False,
                                      preferred_key=identity_key)
            row = self.conn.execute("SELECT recording_id,identity_key FROM catalog_tracks WHERE id=?", (entry_id,)).fetchone()
            rid = row["recording_id"]
            catalog.add_alias(self.conn, identity_key, rid, "discovery")
            self.conn.execute(
                "INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) "
                "ON CONFLICT(recording_id) DO NOTHING", (rid,entry_id,now))
            if source_browse_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO discovery_sources VALUES (?,?,?,?)",
                    (rid,source_browse_id,video_id or None,now))
                if self.conn.execute("SELECT 1 FROM discovered_albums WHERE browse_id=?", (source_browse_id,)).fetchone():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO album_track_links(browse_id,recording_id,entry_id,video_id,source) "
                        "VALUES (?,?,?,?,'discovery')", (source_browse_id,rid,entry_id,video_id or None))
            self.conn.execute(
                "UPDATE catalog_tracks SET genre=COALESCE(genre,?),mb_year=COALESCE(mb_year,?) WHERE id=?",
                (genre,year,entry_id))
            for field, value in (audio or {}).items():
                if field in _DTRACK_AUDIO and value is not None:
                    self.conn.execute(f"UPDATE catalog_tracks SET {field}=COALESCE({field},?) WHERE id=?", (value,entry_id))
            for field,value in {'genre':genre,'mb_year':year,**(audio or {})}.items():
                if value is not None and field in {'genre','mb_year',*_DTRACK_AUDIO}:
                    self.conn.execute(
                        'INSERT OR IGNORE INTO catalog_field_hints(entry_id,field,source,value_json) VALUES (?,?,?,?)',
                        (entry_id,field,source_browse_id or 'discovery',json.dumps(value)))
            return self.conn.execute("SELECT identity_key FROM recordings WHERE id=?", (rid,)).fetchone()[0]

    @staticmethod
    def _dtrack_row(r) -> dict:
        d = {k: r[k] for k in ("id", "recording_id", "identity_key", "video_id", "title", "artist", "album", "thumbnail",
                               "genre", "year", "source_browse_id", "found_at",
                               "first_shown", "last_shown", "offered_count", "last_enriched")}
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
    def record_discovery_impressions(self, kind, ids, view_id, now) -> None:
        """Count each visible Home Discover tile once per page visit, including across poll retries."""
        tbl, col = {"album": ("discovered_albums", "browse_id"),
                    "artist": ("discovered_artists", "artist")}[kind]
        shown = []
        for item_id in dict.fromkeys(ids):
            if not self.conn.execute(f"SELECT 1 FROM {tbl} WHERE {col}=?", (item_id,)).fetchone():
                continue
            inserted = self.conn.execute(
                "INSERT OR IGNORE INTO discovery_visible_impressions(view_id, kind, item_id, shown_at) "
                "VALUES (?,?,?,?)", (view_id, kind, item_id, now))
            if inserted.rowcount:
                shown.append(item_id)
        self.mark_offered(kind, shown, now)

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

    # --- Discovery persistence used by the shared enrichment worker. ----------------------------
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
        count, saved metadata, and expiry (using found_at until shown). Newest first."""
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
                     gc_at=self._gc_at(r["first_shown"] if r["first_shown"] is not None
                                       else r["found_at"], gc_days))
            out.append(d)
        return out

    @synchronized
    def discovery_album_view(self, now, gc_days) -> list:
        rows = self.conn.execute("SELECT * FROM discovered_albums ORDER BY found_at DESC").fetchall()
        return [{**{k: r[k] for k in ("browse_id", "artist", "title", "year", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count", "legacy_offered_count")},
                 "gc_at": self._gc_at(r["first_shown"], gc_days)} for r in rows]

    @synchronized
    def discovery_artist_view(self, now, gc_days) -> list:
        rows = self.conn.execute("SELECT * FROM discovered_artists ORDER BY found_at DESC").fetchall()
        return [{**{k: r[k] for k in ("artist", "score", "thumbnail", "genre",
                                      "found_at", "first_shown", "offered_count", "legacy_offered_count")},
                 "gc_at": self._gc_at(r["first_shown"], gc_days)} for r in rows]

    @synchronized
    def gc_discovery_pool(self, now, gc_days, held_track_keys) -> dict:
        """Expire old suggestions. Unshown tracks age from discovery instead of living forever.

        Albums/artists retain their first-view lifetime. Tracks in live generated playlists are held.
        Purge removed tracks from materialized recommendations in the same transaction.
        """
        cutoff = now - gc_days * 86400
        out = {}
        for key, tbl in (("albums", "discovered_albums"), ("artists", "discovered_artists")):
            cur = self.conn.execute(
                f"DELETE FROM {tbl} WHERE first_shown IS NOT NULL AND first_shown < ?", (cutoff,))
            out[key] = cur.rowcount
        held = set(held_track_keys or ())
        rows = self.conn.execute(
            "SELECT identity_key,recording_id FROM discovered_tracks "
            "WHERE COALESCE(first_shown, found_at) < ?",
            (cutoff,)).fetchall()
        gone = [r["identity_key"] for r in rows if r["identity_key"] not in held]
        if gone:
            # Keep successful album-fetch markers after expiry, so the next discovery tick cannot
            # immediately refill an expired album. These are fetch history, never queue entries.
            source_tokens = {s[0] for r in rows if r['identity_key'] in gone
                             for s in self.conn.execute(
                                 'SELECT source_token FROM discovery_sources WHERE recording_id=?',
                                 (r['recording_id'],))}
            self.conn.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES (?, '1')",
                                  [("discovery_tracks_fetched:" + token,) for token in source_tokens])
            self.conn.executemany("DELETE FROM discovered_tracks WHERE identity_key=?", [(k,) for k in gone])
            self.conn.executemany("DELETE FROM settings WHERE key=?",
                                  [("enrich_attempts:" + k,) for k in gone
                                   if not self.conn.execute("SELECT 1 FROM tracks WHERE identity_key=?", (k,)).fetchone()])
            gone_keys = set(gone)

            def prune(value):
                if isinstance(value, list):
                    return [prune(item) for item in value if not isinstance(item, dict)
                            or item.get("key", item.get("identity_key")) not in gone_keys]
                if isinstance(value, dict):
                    return {key: prune(item) for key, item in value.items()}
                return value

            for row in self.conn.execute("SELECT surface, payload FROM rec_proposals").fetchall():
                payload = json.loads(row["payload"])
                cleaned = prune(payload)
                if cleaned != payload:
                    self.conn.execute("UPDATE rec_proposals SET payload=? WHERE surface=?",
                                      (json.dumps(cleaned), row["surface"]))
        out["tracks"] = len(gone)
        self.conn.execute("DELETE FROM discovery_visible_impressions WHERE shown_at < ?",
                          (now - 90 * 86400,))
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
