"""CollectionRepo: saved-album CRUD plus the aggregate playlist and saved-album
library views that power the collection pages.
"""
import json

from yt_playlist.repos.base import Repo, synchronized


class CollectionRepo(Repo):
    @synchronized
    def cache_album_tracklist(self, browse_id, title, artist, thumbnail, keys, *, video_ids=None):
        """Only full album fetches may write this denominator; playlist sync cannot."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO album_tracklists VALUES (?,?,?,?,?)",
                (browse_id, title, artist, thumbnail, json.dumps(keys)))
            self.conn.execute("DELETE FROM album_share_snapshots WHERE browse_id=?", (browse_id,))
            if video_ids:
                self.conn.execute("INSERT INTO album_share_snapshots VALUES (?,?,?)",
                                  (browse_id, title, json.dumps(video_ids)))

    @synchronized
    def cached_album_share(self, browse_id):
        if browse_id not in self.complete_album_tracklist_ids():
            return None
        row = self.conn.execute("SELECT title,video_ids_json FROM album_share_snapshots WHERE browse_id=?",
                                (browse_id,)).fetchone()
        return {"title": row["title"], "video_ids": json.loads(row["video_ids_json"])} if row else None

    @synchronized
    def cached_album_tracklist_ids(self):
        return {r[0] for r in self.conn.execute("SELECT browse_id FROM album_tracklists")}

    @synchronized
    def complete_album_tracklist_ids(self):
        """Fetched metadata alone does not mean every track was folded into the library."""
        materialized = {}
        for row in self.conn.execute(
                "SELECT a.browse_id,t.identity_key FROM album_track_links a JOIN tracks t ON t.id=a.entry_id"):
            materialized.setdefault(row[0], set()).add(row[1])
        return {row[0] for row in self.conn.execute("SELECT browse_id, track_keys FROM album_tracklists")
                if set(json.loads(row[1])) <= materialized.get(row[0], set())}

    @synchronized
    def playback_source(self, playlist_ytm_id=None, video_id=None, album_browse_id=None) -> dict | None:
        """Resolve the actual playback context, without guessing from playlist membership."""
        if playlist_ytm_id:
            row = self.conn.execute(
                "SELECT p.id, p.title FROM playlists p JOIN identities i ON i.id=p.identity_id "
                "WHERE p.ytm_playlist_id=? ORDER BY i.is_master DESC, p.id LIMIT 1",
                (playlist_ytm_id,)).fetchone()
            if row:
                return {"kind": "playlist", "id": row["id"], "title": row["title"] or "Playlist"}
            # Album queues use OLAK ids. An unrelated playlist or radio queue must not be
            # presented as the track's album just because its songs also appear on that album.
            if not playlist_ytm_id.startswith("OLAK5uy_"):
                return None
        if not album_browse_id:
            row = self.conn.execute(
                "SELECT album_browse_id FROM tracks WHERE video_id=? "
                "AND NULLIF(album_browse_id, '') IS NOT NULL ORDER BY id LIMIT 1",
                (video_id,)).fetchone()
            album_browse_id = row["album_browse_id"] if row else None
        if album_browse_id:
            row = self.conn.execute(
                "SELECT title FROM saved_albums WHERE browse_id=? AND NULLIF(title, '') IS NOT NULL UNION ALL "
                "SELECT album title FROM tracks WHERE album_browse_id=? AND NULLIF(album, '') IS NOT NULL "
                "LIMIT 1", (album_browse_id, album_browse_id)).fetchone()
            return {"kind": "album", "browse": album_browse_id,
                    "title": (row["title"] if row else None) or "Album"}
        return None

    # --- saved albums (mirror of the YouTube Music library, synced locally) ---
    @synchronized
    def replace_saved_albums(self, albums) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM saved_albums")
            self.conn.executemany(
                "INSERT OR REPLACE INTO saved_albums(browse_id,title,artist,year,type,thumbnail) "
                "VALUES (?,?,?,?,?,?)",
                [(a["browse"], a.get("title"), a.get("artist"), str(a.get("year") or ""),
                  a.get("type"), a.get("thumbnail")) for a in albums if a.get("browse")])

    @synchronized
    def saved_album_ids(self) -> set:
        return {r["browse_id"] for r in self.conn.execute("SELECT browse_id FROM saved_albums")}

    @synchronized
    def add_saved_album(self, a) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO saved_albums(browse_id,title,artist,year,type,thumbnail) VALUES (?,?,?,?,?,?)",
            (a["browse"], a.get("title"), a.get("artist"), str(a.get("year") or ""),
             a.get("type"), a.get("thumbnail")))
        self.conn.commit()

    @synchronized
    def remove_saved_album(self, browse_id) -> None:
        self.conn.execute("DELETE FROM saved_albums WHERE browse_id=?", (browse_id,))
        self.conn.commit()

    @synchronized
    def get_saved_albums(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT browse_id browse, title, artist, year, type, thumbnail FROM saved_albums "
            "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def saved_albums_recency(self) -> dict:
        """{browse_id: newest play ts across the album's tracks | None}. Plays come from history (a
        track's identity_key in a snapshot). LEFT JOINs throughout, so an album whose tracks aren't in
        the library (or were never played) comes back None, i.e. as cold as possible."""
        rows = self.conn.execute(
            "SELECT sa.browse_id AS browse, MAX(hs.taken_at) AS last "
            "FROM saved_albums sa "
            "LEFT JOIN tracks t ON t.album_browse_id = sa.browse_id "
            "LEFT JOIN history_items hi ON hi.identity_key = t.identity_key "
            "LEFT JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "GROUP BY sa.browse_id").fetchall()
        return {r["browse"]: r["last"] for r in rows}

    # --- aggregate library views ---
    def _play_counts(self):
        return {r["identity_key"]: r["c"]
                for r in self.conn.execute("SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key")}

    @synchronized
    def collection_albums(self) -> list[dict]:
        """Every album across your playlists: artist, song count, #playlists, total plays."""
        plays = self._play_counts()
        rows = self.conn.execute(
            "SELECT t.album album, t.identity_key key, MIN(t.artist) artist, MIN(t.album_browse_id) browse, "
            "       MIN(t.thumbnail) thumb, GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            "FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
            "WHERE t.album IS NOT NULL AND t.album<>'' GROUP BY t.album, t.identity_key").fetchall()
        albums = {}
        for r in rows:
            a = albums.setdefault(r["album"], {"album": r["album"], "artist": r["artist"],
                                               "browse": r["browse"], "thumb": None,
                                               "songs": 0, "plays": 0, "_pls": set()})
            a["songs"] += 1
            a["plays"] += plays.get(r["key"], 0)
            a["thumb"] = a["thumb"] or r["thumb"]
            a["_pls"].update((r["pls"] or "").split(","))
        out = []
        for a in albums.values():
            a["n_pls"] = len([x for x in a.pop("_pls") if x])
            out.append(a)
        out.sort(key=lambda a: (-a["plays"], a["album"].lower()))
        return out

    @synchronized
    def collection_artists(self) -> list[dict]:
        """Artists across playlists and saved albums, including albums not yet materialized."""
        plays = self._play_counts()
        rows = self.conn.execute(
            "SELECT t.artist artist, t.identity_key key, t.album album, t.artist_browse_id browse, "
            "       t.album_browse_id album_browse, MIN(NULLIF(t.thumbnail,'')) thumb, "
            "       GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            "FROM tracks t LEFT JOIN playlist_tracks pt ON pt.track_id=t.id "
            "WHERE t.artist<>'' AND (pt.track_id IS NOT NULL OR "
            "t.album_browse_id IN (SELECT browse_id FROM saved_albums)) "
            "GROUP BY t.artist, t.artist_browse_id, t.identity_key, t.album, t.album_browse_id").fetchall()
        known_ids = {}
        album_artist_ids = {}
        for r in rows:
            if r["browse"]:
                known_ids.setdefault(r["artist"], set()).add(r["browse"])
                album_artist_ids.setdefault((r["artist"], r["album_browse"]), set()).add(r["browse"])
        artists = {}

        def artist_row(artist, browse=None):
            # Older syncs may not have stored an ID. Fold those tracks into the only known identity;
            # keep them separate when the name genuinely maps to several YouTube artists.
            if not browse and len(known_ids.get(artist, ())) == 1:
                browse = next(iter(known_ids[artist]))
            return artists.setdefault((artist, browse), {
                "artist": artist, "browse": browse, "plays": 0, "thumbnail": None,
                "_songs": set(), "_albums": set(), "_pls": set()})

        for r in rows:
            a = artist_row(r["artist"], r["browse"])
            if r["key"] not in a["_songs"]:
                a["plays"] += plays.get(r["key"], 0)
            a["_songs"].add(r["key"])
            if r["album"] or r["album_browse"]:
                a["_albums"].add((r["album_browse"], (r["album"] or "").strip().casefold()))
            a["thumbnail"] = a["thumbnail"] or r["thumb"]
            a["_pls"].update((r["pls"] or "").split(","))

        for album in self.get_saved_albums():
            if not album["artist"]:
                continue
            ids = album_artist_ids.get((album["artist"], album["browse"]), set())
            a = artist_row(album["artist"], next(iter(ids)) if len(ids) == 1 else None)
            a["_albums"].add((album["browse"], (album["title"] or "").strip().casefold()))
            a["thumbnail"] = a["thumbnail"] or album["thumbnail"]

        out = []
        for a in artists.values():
            a["songs"] = len(a.pop("_songs"))
            albums = a.pop("_albums")
            album_ids = {browse for browse, title in albums if browse}
            identified_titles = {title for browse, title in albums if browse}
            # Match old title-only playlist metadata to identified releases, while keeping
            # separate releases with the same title distinct when their IDs are known.
            title_only = {title for browse, title in albums if not browse}
            a["n_albums"] = len(album_ids) + len(title_only - identified_titles)
            a["n_pls"] = len([x for x in a.pop("_pls") if x])
            out.append(a)
        out.sort(key=lambda a: (-a["plays"], a["artist"].lower()))
        return out

    @synchronized
    def artist_browse_id(self, artist):
        """The artist's YouTube channel/browse id (most common among their tracks), or None."""
        r = self.conn.execute(
            "SELECT artist_browse_id b FROM tracks WHERE artist=? AND artist_browse_id IS NOT NULL "
            "GROUP BY artist_browse_id ORDER BY COUNT(*) DESC LIMIT 1", (artist,)).fetchone()
        return r["b"] if r else None
