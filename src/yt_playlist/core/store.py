import sqlite3
import threading

# Per-domain DAOs split out of this former god class. `_synchronized` lives with them now; Store
# composes the DAOs and delegates legacy `store.X()` calls to them via __getattr__ (see below).
from yt_playlist.repos.actions import ActionRepo
from yt_playlist.repos.base import synchronized as _synchronized
from yt_playlist.repos.charts import ChartsRepo
from yt_playlist.repos.collection import CollectionRepo
from yt_playlist.repos.enrichment import EnrichmentRepo
from yt_playlist.repos.genres import GenreRepo
from yt_playlist.repos.history import HistoryRepo
from yt_playlist.repos.identities import IdentityRepo
from yt_playlist.repos.modes import ModesRepo
from yt_playlist.repos.overlaps import OverlapRepo
from yt_playlist.repos.playlists import PlaylistRepo
from yt_playlist.repos.player_events import PlayerEventsRepo
from yt_playlist.repos.rec import RecRepo
from yt_playlist.repos.road_trip import RoadTripRepo
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.search import SearchRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.repos.tracks import TrackRepo
from yt_playlist.repos.trends import TrendsRepo
from yt_playlist.repos.wiki import WikiRepo

# Row dataclasses live in repos.models (avoids a Store<->repo cycle); re-exported here so existing
# `from yt_playlist.core.store import Playlist` callers keep working.
from yt_playlist.repos.models import Action, Identity, Playlist, Track  # noqa: E402


class Store:
    def __init__(self, db_path, *, backup_dir=None):
        # check_same_thread=False: FastAPI serves sync routes from a threadpool, so the connection
        # is touched from multiple threads. Access is serialized by self._lock (see _synchronized).
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Punctuation/space/accent-insensitive search key (see matching.search_squash): lets
        # cluster_search match 'LSD' against a title stored as 'L.S.D.' (#48).
        from yt_playlist.util.matching import search_squash
        self.conn.create_function("searchnorm", 1, search_squash, deterministic=True)
        self._lock = threading.RLock()
        self._backup_dir = backup_dir

    def _bind_repositories(self):
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
        self.player_events = PlayerEventsRepo(self)
        self.discovery = DiscoveryRepo(self)
        self.search = SearchRepo(self)
        self.enrichment = EnrichmentRepo(self)
        self.wiki = WikiRepo(self)
        self.modes = ModesRepo(self)
        self.trends = TrendsRepo(self)
        self.road_trip = RoadTripRepo(self)
        self._repos = (self.overlaps, self.discovery, self.genres, self.settings, self.actions,
                       self.identities, self.history, self.collection, self.rec, self.charts,
                       self.tracks, self.playlists, self.player_events, self.search, self.enrichment, self.wiki, self.modes,
                       self.trends, self.road_trip)

    def __getattr__(self, name):
        # Delegate any attribute Store no longer defines to the DAO that owns it. Only hit on a
        # miss; __dict__.get avoids recursion before _repos is set during __init__.
        for repo in self.__dict__.get("_repos", ()):
            attr = getattr(repo, name, None)
            if attr is not None:
                return attr
        if '_repos' not in self.__dict__:
            raise AttributeError(f"Store is not initialized; call init_schema() before using {name!r}")
        raise AttributeError(f"{type(self).__name__!r} has no attribute {name!r}")

    @_synchronized
    def init_schema(self):
        from yt_playlist.core.migrations import migrate
        try:
            migrate(self.conn, backup_dir=self._backup_dir)
            if "_repos" not in self.__dict__:
                self._bind_repositories()
            from yt_playlist.core import repair
            repair.run_once(self)
        except BaseException:
            self.conn.close()
            raise
