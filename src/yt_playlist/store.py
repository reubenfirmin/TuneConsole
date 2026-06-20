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
                     video_type=None) -> int:
        key = identity_key(title, artist)
        row = self.conn.execute(
            "SELECT id FROM tracks WHERE identity_key=? AND IFNULL(video_id,'')=IFNULL(?,'')",
            (key, video_id)).fetchone()
        if row:
            if available is not None:   # keep availability fresh on re-sync
                self.conn.execute("UPDATE tracks SET available=? WHERE id=?", (int(available), row["id"]))
            if video_type is not None:
                self.conn.execute("UPDATE tracks SET video_type=? WHERE id=?", (video_type, row["id"]))
            self.conn.commit()
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO tracks(video_id,title,artist,album,duration_s,identity_key,available,video_type) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (video_id, title, artist, album, duration_s, key,
             None if available is None else int(available), video_type))
        self.conn.commit()
        return cur.lastrowid

    @_synchronized
    def playlist_kind(self, playlist_id) -> str:
        """Classify a playlist by its tracks' YouTube videoType: 'audio', 'video', 'mixed', or ''.

        ATV (album track / provided audio) is audio; OMV/UGC/etc. are video. Unknown if no
        videoType has been captured yet.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT t.video_type FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "WHERE pt.playlist_id=? AND t.video_type IS NOT NULL", (playlist_id,)).fetchall()
        kinds = {"audio" if r["video_type"] == "MUSIC_VIDEO_TYPE_ATV" else "video" for r in rows}
        if not kinds:
            return ""
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
        with self.conn:
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self.conn.executemany(
                "INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES (?,?,?)",
                [(playlist_id, tid, pos) for pos, tid in enumerate(track_ids)])

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
