"""DiscoveryRepo: the background outward-discovery state: which artists we've scanned (and when),
and the accumulating pools of new albums / new artists we've found, with last-shown bookkeeping.

Tables are created by numbered migrations. The interest signal (which artists are worth
scanning) is computed live from your library: play counts, playlist participation, saved albums.
"""
import json

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.core import catalog, artists, albums
from yt_playlist.core.discovery_sources import retain as retain_source

# Audio/content columns persisted per discovered track (mirrors the library `tracks` audio set).
_DTRACK_AUDIO = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                 "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
                 "music_key", "music_scale")

_GENERATED_PL = "(SELECT ytm FROM quarantined_playlists)"


class DiscoveryRepo(Repo):

    # --- interest: every artist you engage with, ranked by engagement intensity. Weights ramp with how
    # deliberate the act is: a play is passive (1), filing into a playlist is active curation (2), saving
    # a whole album is the strongest endorsement (3). Tune the multipliers in the SELECT below. ---
    @synchronized
    def interested_artists(self, limit=None) -> list:
        """Engaged artists, highest interest first. `limit` (#52) keeps only the top-N so discovery
        scans a focused set instead of every artist you have ever touched."""
        rows = self.conn.execute(
            "WITH plays AS (SELECT ta.artist_id a,COUNT(DISTINCT hi.rowid) n FROM listening_daily_items hi "
            "                 JOIN tracks t ON t.identity_key=hi.identity_key "
            "                 JOIN track_artists ta ON ta.entry_id=t.id GROUP BY ta.artist_id), "
            "     pls AS (SELECT ta.artist_id a, COUNT(DISTINCT pt.playlist_id) n "
            "               FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "               JOIN track_artists ta ON ta.entry_id=t.id "
            "               JOIN playlists p ON p.id=pt.playlist_id "
            f"              WHERE p.scope_ref NOT IN {_GENERATED_PL} "
            "               GROUP BY ta.artist_id), "
            "     sv AS (SELECT aa.artist_id a,COUNT(DISTINCT m.album_id) n FROM album_library_memberships m "
            "            JOIN album_artists aa ON aa.album_id=m.album_id GROUP BY aa.artist_id), "
            "     arts AS (SELECT a FROM plays UNION SELECT a FROM pls UNION SELECT a FROM sv) "
            "SELECT a.name artist,a.id artist_id,ap.provider_id browse_id, "
            "       COALESCE(plays.n,0)*1.0 + COALESCE(pls.n,0)*2.0 + COALESCE(sv.n,0)*3.0 score "
            "FROM arts JOIN artists a ON a.id=arts.a LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=a.id AND ap.provider='youtube' "
            "LEFT JOIN plays ON plays.a=arts.a LEFT JOIN pls ON pls.a=arts.a "
            "LEFT JOIN sv ON sv.a=arts.a ORDER BY score DESC"
            + (" LIMIT ?" if limit is not None else ""),
            (limit,) if limit is not None else ()).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def artists_due_for_scan(self, now, ttl_days=5, budget=25, artist_limit=None, *, with_ids=False) -> list:
        """Interested artists not scanned within ttl_days (never-scanned first, then oldest), capped
        at budget, exactly which artists the next worker pass should hit the network for. `artist_limit`
        (#52) restricts the universe to your top-N most-engaged artists, so discovery stays focused.

        ttl_days=5 re-scans an artist about weekly (their catalogue changes slowly); budget=25 rate-
        limits each pass so we don't burst the Last.fm/MusicBrainz APIs."""
        cutoff = now - ttl_days * 86400
        scanned = {r["artist_id"]: r["scanned_at"] for r in
                   self.conn.execute("SELECT artist_id, scanned_at FROM artist_scans")}
        due = [a for a in self.interested_artists(limit=artist_limit)
               if a["artist_id"] not in scanned or (scanned[a["artist_id"]] or 0.0) < cutoff]
        due.sort(key=lambda a: (scanned.get(a["artist_id"]) or float("-inf"), -a["score"]))
        return due[:budget] if with_ids else [a['artist'] for a in due[:budget]]

    @synchronized
    def discovered_albums_for_artist(self, artist) -> dict:
        """{browse_id: offered_count} for one artist's pooled albums (#52 rotation input)."""
        aid = artists.lookup(self.conn,artist)
        return {r["browse_id"]: (r["offered_count"] or 0) for r in self.conn.execute(
            "SELECT browse_id, offered_count FROM discovered_albums WHERE artist_id=?", (aid,))}

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
            "AND (s.source_type<>'album' "
            "OR s.source_id IN (SELECT browse_id FROM discovered_albums)))").fetchall()
        held = set(held_keys)
        gone = [(r["identity_key"],) for r in rows if r["identity_key"] not in held]
        self.conn.executemany("DELETE FROM discovered_tracks WHERE identity_key=?", gone)
        self.conn.commit()
        return len(gone)

    @synchronized
    def mark_scanned(self, artist, now) -> None:
        aid = artists.state_id(self.conn,artist)
        self.conn.execute("INSERT INTO artist_scans(artist, scanned_at,artist_id) VALUES (?,?,?) "
                          "ON CONFLICT(artist_id) DO UPDATE SET scanned_at=excluded.scanned_at",
                          (artists.token(aid),now,aid))
        self.conn.commit()

    # --- the accumulating pools ---
    @synchronized
    def upsert_discovered_album(self, browse_id, artist, title, year, thumbnail, now, genre=None, *, artist_id=None) -> None:
        """Add an album to the pool (first-seen found_at is preserved on re-discovery). `genre` is the
        candidate genre for the #18 facet overlay; a later re-discovery with a genre fills it in."""
        aid = artists.state_id(self.conn,artist_id if artist_id is not None else artist)
        with self.conn:
            album_id = albums.observe(self.conn,browse_id,title=title,artist=artist,year=year,
                                      thumbnail=thumbnail,genre=genre,artist_id=aid,rank=1)
            self.conn.execute('INSERT INTO album_discovery_memberships(album_id,found_at) VALUES (?,?) ON CONFLICT(album_id) DO NOTHING', (album_id,now))

    @synchronized
    def upsert_discovered_artist(self, artist, score, because, fits, thumbnail, now, genre=None, *, artist_id=None,
                                 source='discovery') -> None:
        aid = artists.state_id(self.conn,artist_id if artist_id is not None else artist)
        from yt_playlist.core.discovery_sources import retain_artist
        with self.conn:
            retain_artist(self.conn,aid,source,score)
            self.conn.execute(
                "INSERT INTO discovered_artists(artist, score, because, fits, thumbnail, found_at, genre,artist_id) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(artist_id) DO UPDATE SET score=excluded.score, "
                "because=excluded.because, fits=excluded.fits, thumbnail=excluded.thumbnail, "
                "genre=COALESCE(excluded.genre, discovered_artists.genre)",
                (artists.token(aid), score, json.dumps(because or []), json.dumps(fits or []), thumbnail, now, genre,aid))

    @synchronized
    def retain_discovered_artist(self, artist, score):
        """Retain independent interest without overwriting an existing suggestion's metadata."""
        from yt_playlist.core.discovery_sources import retain_artist
        aid = artists.lookup(self.conn,artist,exact=True)
        if aid is not None and self.conn.execute(
                'SELECT 1 FROM discovered_artists WHERE artist_id=?', (aid,)).fetchone():
            with self.conn:
                if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_artist_sources'").fetchone():
                    return
                # When an independent import adopts a Spotify-only suggestion,
                # its score must also survive without retaining Spotify's count.
                self.conn.execute('UPDATE discovered_artists SET score=? WHERE artist_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    "AND source NOT LIKE 'spotify_export:%' AND source<>'spotify_model')", (score,aid,aid))
                retain_artist(self.conn,aid,'discovery',score)

    @synchronized
    def seed_spotify_artists(self, identity_id, counts, now, *, min_plays=3):
        """Retain import provenance even when an artist was independently discovered."""
        from yt_playlist.core.discovery_sources import retain_artist
        seeded = 0
        with self.conn:
            for name, plays in counts.items():
                if plays < min_plays:
                    continue
                aid = artists.state_id(self.conn,name)
                retain_artist(self.conn,aid,f'spotify_export:{identity_id}',float(plays),identity_id)
                seeded += self.conn.execute(
                    'INSERT INTO discovered_artists(artist,artist_id,score,found_at,because,fits) '
                    "VALUES (?,?,?,?, '[]','[]') ON CONFLICT(artist_id) DO NOTHING",
                    (artists.token(aid),aid,float(plays),now)).rowcount
                self.conn.execute('UPDATE discovered_artists SET score=('
                    'SELECT MAX(score) FROM discovery_artist_sources WHERE artist_id=?) WHERE artist_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM discovery_artist_sources WHERE artist_id=? '
                    "AND source NOT LIKE 'spotify_export:%')", (aid,aid,aid))
        return seeded

    @synchronized
    def get_discovered_albums(self) -> list:
        return [{"browse_id": r["browse_id"], "artist": r["artist"], "artist_id":r['artist_id'], "title": r["title"],
                 "year": r["year"], "thumbnail": r["thumbnail"], "found_at": r["found_at"],
                 "last_shown": r["last_shown"], "genre": r["genre"],
                 "first_shown": r["first_shown"], "offered_count": r["offered_count"]}
                for r in self.conn.execute("SELECT * FROM discovered_albums")]

    @synchronized
    def get_discovered_artists(self) -> list:
        return [{"artist": r["display_artist"], "artist_id":r['artist_id'], "score": r["score"], "because": json.loads(r["because"] or "[]"),
                 "fits": json.loads(r["fits"] or "[]"), "thumbnail": r["thumbnail"],
                 "found_at": r["found_at"], "last_shown": r["last_shown"], "genre": r["genre"],
                 "first_shown": r["first_shown"], "offered_count": r["offered_count"]}
                for r in self.conn.execute("SELECT d.*,a.name display_artist FROM discovered_artists d JOIN artists a ON a.id=d.artist_id")]

    # --- #13 Phase 2: out-of-corpus candidate tracks (for the Clusters "reach for new music" mode) ---
    @synchronized
    def upsert_discovered_track(self, identity_key, video_id, title, artist, album, thumbnail,
                                genre, year, source_browse_id, now, audio=None, *, credits=None) -> str:
        with self.conn:
            entry_id = catalog.upsert(self.conn, video_id, title, artist, album,
                                      thumbnail=thumbnail, created_at=now, library=False,
                                      preferred_key=identity_key,credits=credits)
            row = self.conn.execute("SELECT recording_id,identity_key FROM catalog_tracks WHERE id=?", (entry_id,)).fetchone()
            rid = row["recording_id"]
            catalog.add_alias(self.conn, identity_key, rid, "discovery")
            self.conn.execute(
                "INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) "
                "ON CONFLICT(recording_id) DO NOTHING", (rid,entry_id,now))
            if source_browse_id:
                retain_source(self.conn, rid, source_browse_id, video_id or None, now)
                if self.conn.execute("SELECT 1 FROM discovered_albums WHERE browse_id=?", (source_browse_id,)).fetchone():
                    albums.link(self.conn,entry_id,source_browse_id,source='discovery')
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

    @synchronized
    def discovery_sources_for_track(self, key) -> list:
        return [dict(row) for row in self.conn.execute(
            "SELECT s.* FROM discovery_sources s JOIN recordings r ON r.id=s.recording_id "
            "WHERE r.identity_key=? ORDER BY s.found_at,s.source_token", (key,))]

    @synchronized
    def discovered_source_tokens(self, source_type=None) -> set:
        return {row[0] for row in self.conn.execute(
            "SELECT DISTINCT source_token FROM discovery_sources"
            + (" WHERE source_type=?" if source_type else ""),
            (source_type,) if source_type else ())}

    @synchronized
    def remove_discovery_source(self, key, source_token, held_keys=()) -> bool:
        """Remove one contribution; only the last source can remove a candidate."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM discovery_sources WHERE source_token=? AND recording_id="
                "(SELECT id FROM recordings WHERE identity_key=?)", (source_token, key))
            if key in set(held_keys):
                return False
            row = self.conn.execute(
                "SELECT d.recording_id FROM discovery_memberships d JOIN recordings r ON r.id=d.recording_id "
                "WHERE r.identity_key=? AND NOT EXISTS(SELECT 1 FROM discovery_sources s "
                "WHERE s.recording_id=d.recording_id)", (key,)).fetchone()
            if row is None:
                return False
            self.conn.execute("DELETE FROM discovery_memberships WHERE recording_id=?", (row[0],))
            self._prune_cached_tracks({key})
            return True

    def _prune_cached_tracks(self, gone_keys):
        def prune(value):
            if isinstance(value, list):
                return [prune(item) for item in value if not isinstance(item, dict)
                        or item.get('key', item.get('identity_key')) not in gone_keys]
            if isinstance(value, dict):
                return {key: prune(item) for key, item in value.items()}
            return value

        for row in self.conn.execute('SELECT surface,payload FROM rec_proposals').fetchall():
            payload = json.loads(row['payload'])
            cleaned = prune(payload)
            if cleaned != payload:
                self.conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?',
                                  (json.dumps(cleaned), row['surface']))

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
        if kind == 'artist':
            col = 'artist_id'
            ids = [artists.lookup(self.conn,i,exact=True) for i in ids]
        self.conn.executemany(
            f"UPDATE {tbl} SET last_shown=?, first_shown=COALESCE(first_shown, ?), "
            f"offered_count=offered_count+1 WHERE {col}=?", [(now, now, i) for i in ids])
        self.conn.commit()

    @synchronized
    def record_discovery_impressions(self, kind, ids, view_id, now) -> None:
        """Count each visible Home Discover tile once per page visit, including across poll retries."""
        tbl, col = {"album": ("discovered_albums", "browse_id"),
                    "artist": ("discovered_artists", "artist"),
                    "track": ("discovered_tracks", "identity_key")}[kind]
        if kind == 'artist':
            col = 'artist_id'
            ids = [artists.lookup(self.conn,i,exact=True) for i in ids]
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
            "  (SELECT COUNT(*) FROM listening_days hi WHERE hi.identity_key=dt.identity_key) plays, "
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
        rows = self.conn.execute("SELECT d.*,a.name display_artist FROM discovered_artists d JOIN artists a ON a.id=d.artist_id ORDER BY found_at DESC").fetchall()
        return [{"artist":r['display_artist'],'artist_id':r['artist_id'], **{k: r[k] for k in ("score", "thumbnail", "genre",
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
            count = self.conn.execute(f'SELECT COUNT(*) FROM {tbl} WHERE first_shown IS NOT NULL AND first_shown < ?', (cutoff,)).fetchone()[0]
            cur = self.conn.execute(
                f"DELETE FROM {tbl} WHERE first_shown IS NOT NULL AND first_shown < ?", (cutoff,))
            out[key] = count
        held = set(held_track_keys or ())
        rows = self.conn.execute(
            "SELECT identity_key,recording_id FROM discovered_tracks "
            "WHERE COALESCE(first_shown, found_at) < ? "
            "AND NOT EXISTS(SELECT 1 FROM discovery_sources s WHERE "
            "s.recording_id=discovered_tracks.recording_id AND s.last_found_at>=?)",
            (cutoff,cutoff)).fetchall()
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
            self._prune_cached_tracks(set(gone))
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
            if r["browse_id"] in saved_browse_ids or albums.lookup(self.conn,r['browse_id']) in owned_albums:
                self.conn.execute("DELETE FROM discovered_albums WHERE browse_id=?", (r["browse_id"],))
        if owned_artists:
            from yt_playlist.util.matching import normalize
            for r in self.conn.execute("SELECT d.artist,d.artist_id,a.name FROM discovered_artists d JOIN artists a ON a.id=d.artist_id").fetchall():
                if artists.token(r['artist_id']) in owned_artists or artists.public_key(self.conn,r['artist_id']) in owned_artists:
                    self.conn.execute("DELETE FROM discovered_artists WHERE artist=?", (r["artist"],))
        self.conn.commit()
