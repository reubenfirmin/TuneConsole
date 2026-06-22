import sqlite3
import threading
from pathlib import Path

# Per-domain DAOs split out of this former god class. `_synchronized` lives with them now; Store
# composes the DAOs and delegates legacy `store.X()` calls to them via __getattr__ (see below).
from yt_playlist.repos.actions import ActionRepo
from yt_playlist.repos.base import synchronized as _synchronized
from yt_playlist.repos.charts import ChartsRepo
from yt_playlist.repos.collection import CollectionRepo
from yt_playlist.repos.genres import GenreRepo
from yt_playlist.repos.history import HistoryRepo
from yt_playlist.repos.identities import IdentityRepo
from yt_playlist.repos.overlaps import OverlapRepo
from yt_playlist.repos.playlists import PlaylistRepo
from yt_playlist.repos.rec import RecRepo
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.search import SearchRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.repos.tracks import TrackRepo

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
  thumbnail TEXT,
  genre TEXT,
  mb_year TEXT,
  UNIQUE(identity_key, video_id)
);
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES identities(id),
  ytm_playlist_id TEXT NOT NULL,
  title TEXT, track_count INTEGER,
  content_hash TEXT,
  first_seen REAL, last_seen REAL, last_changed REAL,
  thumbnail TEXT,
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
CREATE TABLE IF NOT EXISTS cleanup_ignored (
  ytm TEXT NOT NULL, category TEXT NOT NULL,   -- category: 'empty' | 'tiny' (per-playlist, scoped)
  created_at REAL, PRIMARY KEY (ytm, category)
);
CREATE TABLE IF NOT EXISTS ignored_merges (
  signature TEXT PRIMARY KEY,   -- canonical sorted member ytm ids joined ("A|B|C"): one merge suggestion
  members TEXT NOT NULL,        -- JSON list of member ytm ids (for display / reconstruction)
  created_at REAL
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
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT   -- small app settings, e.g. lastfm_api_key
);
CREATE TABLE IF NOT EXISTS genre_whitelist (
  name TEXT PRIMARY KEY COLLATE NOCASE   -- editable genre whitelist for tag matching
);
CREATE TABLE IF NOT EXISTS rec_vectors (
  identity_key TEXT PRIMARY KEY,
  vec BLOB NOT NULL                       -- float32 taste-embedding for the track (see embed.py)
);
CREATE TABLE IF NOT EXISTS rec_feedback (
  surface TEXT NOT NULL,                  -- where it happened: 'for_you', 'suggest', 'discover'
  item_key TEXT NOT NULL,                 -- track identity_key (or 'artist:<name>' for a mute)
  kind TEXT NOT NULL,                     -- 'dismiss' | 'less' | 'more' | 'mute' | 'not_now'
  reason TEXT,                            -- optional axis/reason ('era','artist','vibe','own_it')
  scope TEXT NOT NULL DEFAULT '',         -- '' = global; else a playlist id for playlist-local
  until REAL,                             -- suppressed until ts (NULL = until explicitly cleared)
  created_at REAL,
  PRIMARY KEY (surface, item_key, scope)
);
CREATE TABLE IF NOT EXISTS rec_weights (
  axis TEXT PRIMARY KEY,                  -- e.g. 'lane:deep_cut', 'family:techno'
  weight REAL NOT NULL DEFAULT 1.0        -- learned blend weight; 1.0 = prior
);
"""

# Row dataclasses live in repos.models (avoids a Store<->repo cycle); re-exported here so existing
# `from yt_playlist.core.store import Playlist` callers keep working.
from yt_playlist.repos.models import Action, Identity, Playlist, Track  # noqa: E402


class Store:
    def __init__(self, db_path):
        # check_same_thread=False: FastAPI serves sync routes from a threadpool, so the connection
        # is touched from multiple threads. Access is serialized by self._lock (see _synchronized).
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        # --- domain DAOs (each shares this connection + lock). Use store.overlaps.x() in new code;
        #     legacy store.x() still works via __getattr__ while methods migrate out of Store. ---
        self.overlaps = OverlapRepo(self)
        self.genres = GenreRepo(self)
        self.settings = SettingsRepo(self)
        self.actions = ActionRepo(self)
        self.identities = IdentityRepo(self)
        self.history = HistoryRepo(self)
        self.collection = CollectionRepo(self)
        self.rec = RecRepo(self)
        self.charts = ChartsRepo(self)
        self.tracks = TrackRepo(self)
        self.playlists = PlaylistRepo(self)
        self.discovery = DiscoveryRepo(self)
        self.search = SearchRepo(self)
        self._repos = (self.overlaps, self.discovery, self.genres, self.settings, self.actions,
                       self.identities, self.history, self.collection, self.rec, self.charts,
                       self.tracks, self.playlists, self.search)

    def __getattr__(self, name):
        # Delegate any attribute Store no longer defines to the DAO that owns it. Only hit on a
        # miss; __dict__.get avoids recursion before _repos is set during __init__.
        for repo in self.__dict__.get("_repos", ()):
            attr = getattr(repo, name, None)
            if attr is not None:
                return attr
        raise AttributeError(f"{type(self).__name__!r} has no attribute {name!r}")

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
        if "thumbnail" not in cols:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN thumbnail TEXT")
        if "genre" not in cols:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN genre TEXT")
        if "mb_year" not in cols:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN mb_year TEXT")
        pcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(playlists)")}
        if "thumbnail" not in pcols:
            self.conn.execute("ALTER TABLE playlists ADD COLUMN thumbnail TEXT")
        self.conn.commit()
