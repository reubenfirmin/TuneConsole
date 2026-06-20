import sqlite3
import threading
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from yt_playlist.matching import identity_key


def _synchronized(method):
    """Serialize access to the shared sqlite3 connection.

    FastAPI serves sync routes from a threadpool, so two requests (e.g. a slow /sync and a
    dashboard load) can land on different threads sharing one connection. A single sqlite3
    connection is not safe for concurrent use, so every Store method holds a re-entrant lock.
    The lock is released between calls, so long network-bound work in callers never blocks the DB.
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  brand_account_id TEXT,
  is_master INTEGER NOT NULL DEFAULT 0,
  last_auth_ok REAL,
  UNIQUE(label)
);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  video_id TEXT,
  title TEXT, artist TEXT, album TEXT, duration_s INTEGER,
  identity_key TEXT NOT NULL,
  available INTEGER,
  video_type TEXT,
  artist_browse_id TEXT,
  album_browse_id TEXT,
  UNIQUE(identity_key, video_id)
);
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  ytm_playlist_id TEXT NOT NULL,
  title TEXT, track_count INTEGER,
  content_hash TEXT,
  first_seen REAL, last_seen REAL, last_changed REAL,
  UNIQUE(identity_id, ytm_playlist_id)
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  position INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tracks_null_vid ON tracks(identity_key) WHERE video_id IS NULL;
CREATE TABLE IF NOT EXISTS history_snapshots (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  taken_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history_items (
  snapshot_id INTEGER NOT NULL REFERENCES history_snapshots(id),
  identity_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  params_json TEXT, plan_json TEXT, undo_json TEXT,
  status TEXT NOT NULL,
  created_at REAL, executed_at REAL
);
CREATE TABLE IF NOT EXISTS suppressed_overlaps (
  a TEXT NOT NULL, b TEXT NOT NULL, created_at REAL,
  PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS overlap_ignored (
  ytm TEXT PRIMARY KEY, created_at REAL
);
CREATE TABLE IF NOT EXISTS overlap_kept (
  a TEXT NOT NULL, b TEXT NOT NULL, created_at REAL,
  PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS stale_dismissed (
  ytm TEXT PRIMARY KEY, until REAL   -- until NULL = dismissed forever; else snoozed until ts
);
CREATE TABLE IF NOT EXISTS playlist_group (
  ytm TEXT PRIMARY KEY, name TEXT NOT NULL   -- user-assigned group name for a playlist
);
CREATE TABLE IF NOT EXISTS hidden_playlists (
  ytm TEXT PRIMARY KEY   -- playlists hidden from the Playlists tab (e.g. undeletable system ones)
);
CREATE TABLE IF NOT EXISTS saved_albums (
  browse_id TEXT PRIMARY KEY, title TEXT, artist TEXT, year TEXT, type TEXT, thumbnail TEXT
);
"""

@dataclass
class Identity:
    id: int; label: str; credential_ref: str
    brand_account_id: str | None; is_master: bool; last_auth_ok: float | None

@dataclass
class Playlist:
    id: int; identity_id: int; ytm_playlist_id: str; title: str
    track_count: int; content_hash: str
    first_seen: float; last_seen: float; last_changed: float

@dataclass
class Track:
    id: int; video_id: str | None; title: str; artist: str
    album: str | None; duration_s: int | None; identity_key: str

@dataclass
class Action:
    id: int; kind: str; params_json: str | None; plan_json: str | None; undo_json: str | None
    status: str; created_at: float; executed_at: float | None


class Store:
    def __init__(self, db_path):
        # check_same_thread=False: FastAPI serves sync routes from a threadpool, so the connection
        # is touched from multiple threads. Access is serialized by self._lock (see _synchronized).
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()

    @_synchronized
    def init_schema(self):
        self.conn.executescript(SCHEMA)
        # migrations: add columns to pre-existing databases
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tracks)")}
        if "available" not in cols:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN available INTEGER")
        if "video_type" not in cols:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN video_type TEXT")
        if "artist_browse_id" not in cols:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN artist_browse_id TEXT")
        if "album_browse_id" not in cols:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN album_browse_id TEXT")
        self.conn.commit()

    @_synchronized
    def upsert_identity(self, label, credential_ref, brand_account_id, is_master):
        self.conn.execute(
            "INSERT INTO identities(label, credential_ref, brand_account_id, is_master) "
            "VALUES (?,?,?,?) ON CONFLICT(label) DO UPDATE SET "
            "credential_ref=excluded.credential_ref, "
            "brand_account_id=excluded.brand_account_id, "
            "is_master=excluded.is_master",
            (label, credential_ref, brand_account_id, int(is_master)))
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM identities WHERE label=?", (label,)).fetchone()
        return row["id"]

    @_synchronized
    def get_identities(self) -> list[Identity]:
        rows = self.conn.execute("SELECT * FROM identities").fetchall()
        return [Identity(r["id"], r["label"], r["credential_ref"], r["brand_account_id"],
                         bool(r["is_master"]), r["last_auth_ok"]) for r in rows]

    @_synchronized
    def get_master_identity(self):
        try:
            return next(i for i in self.get_identities() if i.is_master)
        except StopIteration:
            raise ValueError("No master identity configured")

    @_synchronized
    def upsert_track(self, video_id, title, artist, album, duration_s, available=None,
                     video_type=None, artist_browse_id=None, album_browse_id=None) -> int:
        key = identity_key(title, artist)
        row = self.conn.execute(
            "SELECT id FROM tracks WHERE identity_key=? AND IFNULL(video_id,'')=IFNULL(?,'')",
            (key, video_id)).fetchone()
        if row:
            # keep these fresh on re-sync (backfills existing rows once the data is available)
            for col, val in (("available", None if available is None else int(available)),
                             ("video_type", video_type),
                             ("artist_browse_id", artist_browse_id),
                             ("album_browse_id", album_browse_id)):
                if val is not None:
                    self.conn.execute(f"UPDATE tracks SET {col}=? WHERE id=?", (val, row["id"]))
            self.conn.commit()
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO tracks(video_id,title,artist,album,duration_s,identity_key,available,"
            "video_type,artist_browse_id,album_browse_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (video_id, title, artist, album, duration_s, key,
             None if available is None else int(available), video_type,
             artist_browse_id, album_browse_id))
        self.conn.commit()
        return cur.lastrowid

    @_synchronized
    def playlist_kind(self, playlist_id) -> str:
        """Classify a playlist by its tracks' YouTube videoType.

        Returns 'audio' (all ATV), 'video' (all OMV/UGC/…), 'mixed' (both), 'mix' (has tracks but
        YouTube tagged none — i.e. an auto-generated radio/mix playlist), or '' (no tracks).
        """
        rows = self.conn.execute(
            "SELECT t.video_type FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=?", (playlist_id,)).fetchall()
        if not rows:
            return ""
        kinds = {"audio" if r["video_type"] == "MUSIC_VIDEO_TYPE_ATV" else "video"
                 for r in rows if r["video_type"] is not None}
        if not kinds:
            return "mix"        # non-empty but entirely untyped -> auto-mix / radio
        if kinds == {"audio"}:
            return "audio"
        if kinds == {"video"}:
            return "video"
        return "mixed"

    @_synchronized
    def upsert_playlist(self, identity_id, ytm_playlist_id, title, track_count, content_hash, now) -> int:
        row = self.conn.execute(
            "SELECT id, first_seen, content_hash, last_changed FROM playlists "
            "WHERE identity_id=? AND ytm_playlist_id=?", (identity_id, ytm_playlist_id)).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO playlists(identity_id,ytm_playlist_id,title,track_count,"
                "content_hash,first_seen,last_seen,last_changed) VALUES (?,?,?,?,?,?,?,?)",
                (identity_id, ytm_playlist_id, title, track_count, content_hash, now, now, now))
            self.conn.commit()
            return cur.lastrowid
        last_changed = now if row["content_hash"] != content_hash else row["last_changed"]
        self.conn.execute(
            "UPDATE playlists SET title=?, track_count=?, content_hash=?, last_seen=?, last_changed=? "
            "WHERE id=?", (title, track_count, content_hash, now, last_changed, row["id"]))
        self.conn.commit()
        return row["id"]

    @_synchronized
    def set_playlist_tracks(self, playlist_id, track_ids) -> None:
        # de-dupe by track id, keeping first position — YouTube's get_playlist can return the same
        # video many times (pagination duplication), which would otherwise inflate the playlist.
        seen = set()
        unique = [t for t in track_ids if not (t in seen or seen.add(t))]
        with self.conn:
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self.conn.executemany(
                "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES (?,?,?)",
                [(playlist_id, tid, pos) for pos, tid in enumerate(unique)])

    @_synchronized
    def remove_playlist(self, playlist_id) -> None:
        """Drop a playlist, its track links, and any overlap prefs that referenced it.

        Pruning the suppress/ignore/keep rows keeps stale pairs (one side deleted) from
        lingering in the Hidden/Ignored sections.
        """
        with self.conn:
            row = self.conn.execute("SELECT ytm_playlist_id FROM playlists WHERE id=?",
                                    (playlist_id,)).fetchone()
            ytm = row["ytm_playlist_id"] if row else None
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self.conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            if ytm is not None:
                self.conn.execute("DELETE FROM suppressed_overlaps WHERE a=? OR b=?", (ytm, ytm))
                self.conn.execute("DELETE FROM overlap_ignored WHERE ytm=?", (ytm,))
                self.conn.execute("DELETE FROM overlap_kept WHERE a=? OR b=?", (ytm, ytm))
                self.conn.execute("DELETE FROM playlist_group WHERE ytm=?", (ytm,))

    @_synchronized
    def get_playlists(self) -> list[Playlist]:
        rows = self.conn.execute("SELECT * FROM playlists").fetchall()
        return [Playlist(r["id"], r["identity_id"], r["ytm_playlist_id"], r["title"],
                         r["track_count"], r["content_hash"], r["first_seen"],
                         r["last_seen"], r["last_changed"]) for r in rows]

    @_synchronized
    def get_playlist(self, playlist_id) -> Playlist | None:
        row = self.conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        return None if row is None else Playlist(
            row["id"], row["identity_id"], row["ytm_playlist_id"], row["title"],
            row["track_count"], row["content_hash"], row["first_seen"],
            row["last_seen"], row["last_changed"])

    @_synchronized
    def get_playlist_track_keys(self, playlist_id) -> set[str]:
        rows = self.conn.execute(
            "SELECT t.identity_key FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=?", (playlist_id,)).fetchall()
        return {r["identity_key"] for r in rows}

    @_synchronized
    def get_playlist_tracks_with_meta(self, playlist_id) -> list[tuple]:
        rows = self.conn.execute(
            "SELECT t.identity_key, t.video_id, t.title, t.artist, t.duration_s, t.available "
            "FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position", (playlist_id,)).fetchall()
        return [(r["identity_key"], r["video_id"], r["title"], r["artist"], r["duration_s"], r["available"])
                for r in rows]

    @_synchronized
    def add_history_snapshot(self, identity_id, taken_at, item_keys) -> int:
        cur = self.conn.execute(
            "INSERT INTO history_snapshots(identity_id,taken_at) VALUES (?,?)",
            (identity_id, taken_at))
        sid = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO history_items(snapshot_id,identity_key) VALUES (?,?)",
            [(sid, k) for k in item_keys])
        self.conn.commit()
        return sid

    @_synchronized
    def get_recent_history_keys(self, since_ts) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT hi.identity_key FROM history_items hi "
            "JOIN history_snapshots hs ON hs.id=hi.snapshot_id WHERE hs.taken_at>=?",
            (since_ts,)).fetchall()
        return {r["identity_key"] for r in rows}

    @_synchronized
    def record_action(self, kind, params_json, plan_json, status, undo_json, created_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO actions(kind,params_json,plan_json,undo_json,status,created_at) "
            "VALUES (?,?,?,?,?,?)", (kind, params_json, plan_json, undo_json, status, created_at))
        self.conn.commit()
        return cur.lastrowid

    @_synchronized
    def get_action(self, action_id) -> Action | None:
        row = self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            return None
        return Action(row["id"], row["kind"], row["params_json"], row["plan_json"],
                      row["undo_json"], row["status"], row["created_at"], row["executed_at"])

    @_synchronized
    def update_action(self, action_id, status, executed_at, undo_json=None) -> None:
        if undo_json is None:
            self.conn.execute("UPDATE actions SET status=?, executed_at=? WHERE id=?",
                              (status, executed_at, action_id))
        else:
            self.conn.execute("UPDATE actions SET status=?, executed_at=?, undo_json=? WHERE id=?",
                              (status, executed_at, undo_json, action_id))
        self.conn.commit()

    @_synchronized
    def suppress_overlap(self, ytm_a, ytm_b, now) -> None:
        a, b = sorted((ytm_a, ytm_b))  # normalize order so the pair is unordered
        self.conn.execute("INSERT OR IGNORE INTO suppressed_overlaps(a,b,created_at) VALUES (?,?,?)",
                          (a, b, now))
        self.conn.commit()

    @_synchronized
    def unsuppress_overlap(self, ytm_a, ytm_b) -> None:
        a, b = sorted((ytm_a, ytm_b))
        self.conn.execute("DELETE FROM suppressed_overlaps WHERE a=? AND b=?", (a, b))
        self.conn.commit()

    @_synchronized
    def get_suppressed_overlap_pairs(self) -> set:
        rows = self.conn.execute("SELECT a,b FROM suppressed_overlaps").fetchall()
        return {frozenset((r["a"], r["b"])) for r in rows}

    @_synchronized
    def get_suppressed_overlaps(self) -> list[tuple]:
        rows = self.conn.execute(
            "SELECT a,b,created_at FROM suppressed_overlaps ORDER BY created_at DESC").fetchall()
        return [(r["a"], r["b"], r["created_at"]) for r in rows]

    @_synchronized
    def ignore_overlap_playlist(self, ytm, now) -> None:
        self.conn.execute("INSERT OR IGNORE INTO overlap_ignored(ytm,created_at) VALUES (?,?)", (ytm, now))
        self.conn.commit()

    @_synchronized
    def unignore_overlap_playlist(self, ytm) -> None:
        self.conn.execute("DELETE FROM overlap_ignored WHERE ytm=?", (ytm,))
        self.conn.commit()

    @_synchronized
    def get_overlap_ignored(self) -> set:
        return {r["ytm"] for r in self.conn.execute("SELECT ytm FROM overlap_ignored").fetchall()}

    @_synchronized
    def keep_overlap_pair(self, ytm_a, ytm_b, now) -> None:
        a, b = sorted((ytm_a, ytm_b))   # pair the user wants to keep visible despite ignoring a playlist
        self.conn.execute("INSERT OR IGNORE INTO overlap_kept(a,b,created_at) VALUES (?,?,?)", (a, b, now))
        self.conn.commit()

    @_synchronized
    def get_overlap_kept_pairs(self) -> set:
        rows = self.conn.execute("SELECT a,b FROM overlap_kept").fetchall()
        return {frozenset((r["a"], r["b"])) for r in rows}

    @_synchronized
    def dismiss_stale(self, ytm, until=None) -> None:
        # until=None → dismissed forever; else a unix-ts the snooze expires at
        self.conn.execute("INSERT OR REPLACE INTO stale_dismissed(ytm,until) VALUES (?,?)", (ytm, until))
        self.conn.commit()

    @_synchronized
    def restore_stale(self, ytm) -> None:
        self.conn.execute("DELETE FROM stale_dismissed WHERE ytm = ?", (ytm,))
        self.conn.commit()

    @_synchronized
    def set_playlist_group(self, ytm, name) -> None:
        name = (name or "").strip()
        if name:
            self.conn.execute("INSERT OR REPLACE INTO playlist_group(ytm,name) VALUES (?,?)", (ytm, name))
        else:   # empty name clears the assignment
            self.conn.execute("DELETE FROM playlist_group WHERE ytm=?", (ytm,))
        self.conn.commit()

    @_synchronized
    def hide_playlist(self, ytm) -> None:
        self.conn.execute("INSERT OR IGNORE INTO hidden_playlists(ytm) VALUES (?)", (ytm,))
        self.conn.commit()

    @_synchronized
    def unhide_playlist(self, ytm) -> None:
        self.conn.execute("DELETE FROM hidden_playlists WHERE ytm=?", (ytm,))
        self.conn.commit()

    @_synchronized
    def get_hidden_playlists(self) -> set:
        return {r["ytm"] for r in self.conn.execute("SELECT ytm FROM hidden_playlists")}

    @_synchronized
    def replace_saved_albums(self, albums) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM saved_albums")
            self.conn.executemany(
                "INSERT OR REPLACE INTO saved_albums(browse_id,title,artist,year,type,thumbnail) "
                "VALUES (?,?,?,?,?,?)",
                [(a["browse"], a.get("title"), a.get("artist"), str(a.get("year") or ""),
                  a.get("type"), a.get("thumbnail")) for a in albums if a.get("browse")])

    @_synchronized
    def get_saved_albums(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT browse_id browse, title, artist, year, type, thumbnail FROM saved_albums "
            "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def get_playlist_groups(self) -> dict:
        return {r["ytm"]: r["name"] for r in self.conn.execute("SELECT ytm,name FROM playlist_group")}

    @_synchronized
    def get_playlist_listen_stats(self) -> dict:
        """Per-playlist {playlist_id: (last_listen_ts | None, listen_count)} from sync history.

        listen_count = times the playlist's tracks appear across history snapshots; last = newest
        snapshot containing any of them. Playlists with no recorded listens are absent.
        """
        rows = self.conn.execute(
            "SELECT pt.playlist_id AS pid, COUNT(hi.identity_key) AS cnt, MAX(hs.taken_at) AS last "
            "FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.track_id "
            "JOIN history_items hi ON hi.identity_key = t.identity_key "
            "JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
            "GROUP BY pt.playlist_id").fetchall()
        return {r["pid"]: (r["last"], r["cnt"]) for r in rows}

    @_synchronized
    def top_tracks(self, limit=100, since=None) -> list[dict]:
        """Most-played songs from sync history — play count = appearances across history snapshots.

        `since` (unix ts) limits to snapshots at/after that time, for a time-windowed chart.
        """
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key, COUNT(*) c FROM history_items hi "
            "  JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            "  WHERE (:since IS NULL OR hs.taken_at >= :since) GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(title) title, MIN(artist) artist, MIN(video_id) vid "
            "               FROM tracks GROUP BY identity_key) "
            "SELECT n.title, n.artist, n.vid, p.c FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "WHERE n.title <> '' ORDER BY p.c DESC, n.title LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"title": r["title"], "artist": r["artist"], "video_id": r["vid"], "plays": r["c"]}
                for r in rows]

    @_synchronized
    def top_artists(self, limit=100, since=None) -> list[dict]:
        """Most-played artists from sync history — play count summed over the artist's songs."""
        rows = self.conn.execute(
            "WITH plays AS (SELECT hi.identity_key, COUNT(*) c FROM history_items hi "
            "  JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
            "  WHERE (:since IS NULL OR hs.taken_at >= :since) GROUP BY hi.identity_key), "
            "     names AS (SELECT identity_key, MIN(artist) artist FROM tracks GROUP BY identity_key) "
            "SELECT n.artist, SUM(p.c) total FROM plays p JOIN names n ON n.identity_key=p.identity_key "
            "WHERE n.artist <> '' GROUP BY n.artist ORDER BY total DESC, n.artist LIMIT :limit",
            {"since": since, "limit": limit}).fetchall()
        return [{"artist": r["artist"], "plays": r["total"]} for r in rows]

    @_synchronized
    def artist_songs(self, artist) -> list[dict]:
        """An artist's songs that appear in your playlists: play count + which playlists hold each."""
        songs = self.conn.execute(
            "SELECT t.identity_key key, MIN(t.title) title, MIN(t.album) album, MIN(t.video_id) vid, "
            "       MIN(t.duration_s) dur, MIN(t.video_type) vtype, "
            "       (SELECT COUNT(*) FROM history_items hi WHERE hi.identity_key=t.identity_key) plays "
            "FROM tracks t WHERE t.artist=? GROUP BY t.identity_key", (artist,)).fetchall()
        membership = self.conn.execute(
            "SELECT DISTINCT t.identity_key key, pl.title title, pl.ytm_playlist_id ytm FROM tracks t "
            "JOIN playlist_tracks pt ON pt.track_id=t.id JOIN playlists pl ON pl.id=pt.playlist_id "
            "WHERE t.artist=?", (artist,)).fetchall()
        by_key = {}
        for r in membership:
            by_key.setdefault(r["key"], []).append({"title": r["title"], "ytm": r["ytm"]})
        out = [{"title": r["title"], "album": r["album"] or "", "video_id": r["vid"],
                "duration": r["dur"], "plays": r["plays"],
                "playlists": sorted(by_key.get(r["key"], []), key=lambda p: p["title"].lower())}
               for r in songs]
        out.sort(key=lambda s: (-s["plays"], (s["title"] or "").lower()))
        return out

    def _play_counts(self):
        return {r["identity_key"]: r["c"]
                for r in self.conn.execute("SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key")}

    @_synchronized
    def collection_albums(self) -> list[dict]:
        """Every album across your playlists: artist, song count, #playlists, total plays."""
        plays = self._play_counts()
        rows = self.conn.execute(
            "SELECT t.album album, t.identity_key key, MIN(t.artist) artist, MIN(t.album_browse_id) browse, "
            "       GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            "FROM tracks t JOIN playlist_tracks pt ON pt.track_id=t.id "
            "WHERE t.album IS NOT NULL AND t.album<>'' GROUP BY t.album, t.identity_key").fetchall()
        albums = {}
        for r in rows:
            a = albums.setdefault(r["album"], {"album": r["album"], "artist": r["artist"],
                                               "browse": r["browse"], "songs": 0, "plays": 0, "_pls": set()})
            a["songs"] += 1
            a["plays"] += plays.get(r["key"], 0)
            a["_pls"].update((r["pls"] or "").split(","))
        out = []
        for a in albums.values():
            a["n_pls"] = len([x for x in a.pop("_pls") if x])
            out.append(a)
        out.sort(key=lambda a: (-a["plays"], a["album"].lower()))
        return out

    @_synchronized
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
        out = []
        for a in artists.values():
            a["songs"] = scount.get(a["artist"], 0)
            a["n_albums"] = len(a.pop("_albums"))
            a["n_pls"] = len([x for x in a.pop("_pls") if x])
            out.append(a)
        out.sort(key=lambda a: (-a["plays"], a["artist"].lower()))
        return out

    @_synchronized
    def artist_browse_id(self, artist):
        """The artist's YouTube channel/browse id (most common among their tracks), or None."""
        r = self.conn.execute(
            "SELECT artist_browse_id b FROM tracks WHERE artist=? AND artist_browse_id IS NOT NULL "
            "GROUP BY artist_browse_id ORDER BY COUNT(*) DESC LIMIT 1", (artist,)).fetchone()
        return r["b"] if r else None

    @_synchronized
    def get_stale_dismissed(self, now) -> list[tuple]:
        # rows still in effect (forever, or snoozed until > now), as (ytm, until)
        rows = self.conn.execute("SELECT ytm, until FROM stale_dismissed").fetchall()
        return [(r["ytm"], r["until"]) for r in rows if r["until"] is None or r["until"] > now]

    def get_stale_hidden_ytm(self, now) -> set:
        return {ytm for ytm, _ in self.get_stale_dismissed(now)}

    @_synchronized
    def get_actions(self) -> list[Action]:
        rows = self.conn.execute("SELECT * FROM actions ORDER BY id DESC").fetchall()
        return [Action(r["id"], r["kind"], r["params_json"], r["plan_json"], r["undo_json"],
                       r["status"], r["created_at"], r["executed_at"]) for r in rows]
