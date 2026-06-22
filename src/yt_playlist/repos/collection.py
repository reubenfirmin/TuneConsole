"""CollectionRepo — the user's album collection: saved-album CRUD plus the aggregate
library views (every album / artist across all playlists) that power the collection pages.
"""
from yt_playlist.repos.base import Repo, synchronized


class CollectionRepo(Repo):
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

    # --- aggregate library views (computed across every playlist) ---
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
        """Every artist across your playlists: song count, distinct albums, #playlists, total plays."""
        plays = self._play_counts()
        rows = self.conn.execute(
            "SELECT t.artist artist, t.identity_key key, t.album album, MIN(t.artist_browse_id) browse, "
            "       GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            "FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
            "WHERE t.artist<>'' GROUP BY t.artist, t.identity_key, t.album").fetchall()
        artists = {}
        for r in rows:
            a = artists.setdefault(r["artist"], {"artist": r["artist"], "browse": r["browse"],
                                                 "songs": 0, "plays": 0, "_albums": set(), "_pls": set()})
            a["plays"] += plays.get(r["key"], 0)
            if r["album"]:
                a["_albums"].add(r["album"])
            a["_pls"].update((r["pls"] or "").split(","))
        # song count = distinct identity_keys per artist (separate, since the query groups by album too)
        scount = {r["artist"]: r["c"] for r in self.conn.execute(
            "SELECT artist, COUNT(DISTINCT identity_key) c FROM tracks WHERE artist<>'' GROUP BY artist")}
        thumbs = {r["artist"]: r["thumb"] for r in self.conn.execute(
            "SELECT artist, MIN(thumbnail) thumb FROM tracks "
            "WHERE artist<>'' AND thumbnail IS NOT NULL GROUP BY artist")}
        out = []
        for a in artists.values():
            a["songs"] = scount.get(a["artist"], 0)
            a["n_albums"] = len(a.pop("_albums"))
            a["n_pls"] = len([x for x in a.pop("_pls") if x])
            a["thumbnail"] = thumbs.get(a["artist"])
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
