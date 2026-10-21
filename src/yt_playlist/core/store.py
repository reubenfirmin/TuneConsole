import sqlite3
import threading

# Store owns the connection, schema lifecycle, and explicitly named repositories.
from yt_playlist.repos.actions import ActionRepo
from yt_playlist.repos.base import synchronized as _synchronized
from yt_playlist.repos.charts import ChartsRepo
from yt_playlist.repos.collection import CollectionRepo
from yt_playlist.repos.enrichment import EnrichmentRepo
from yt_playlist.repos.genres import GenreRepo
from yt_playlist.repos.history import HistoryRepo
from yt_playlist.repos.identities import IdentityRepo
from yt_playlist.repos.modes import ModesRepo
from yt_playlist.repos.model_selection import ModelSelectionRepo
from yt_playlist.repos.overlaps import OverlapRepo
from yt_playlist.repos.playlists import PlaylistRepo
from yt_playlist.repos.player_events import PlayerEventsRepo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.rec_surface import RecSurfaceRepo
from yt_playlist.repos.road_trip import RoadTripRepo
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.search import SearchRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.repos.tracks import TrackRepo
from yt_playlist.repos.trends import TrendsRepo
from yt_playlist.repos.wiki import WikiRepo

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
        # Each repository shares this connection and lock; consumers name its owner explicitly.
        self.overlaps = OverlapRepo(self)
        self.genres = GenreRepo(self)
        self.settings = SettingsRepo(self)
        self.actions = ActionRepo(self)
        self.identities = IdentityRepo(self)
        self.history = HistoryRepo(self)
        self.collection = CollectionRepo(self)
        self.rec_model = RecModelRepo(self)
        self.model_selection = ModelSelectionRepo(self)
        self.rec_query = RecQueryRepo(self)
        self.rec_surface = RecSurfaceRepo(self)
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

    @_synchronized
    def init_schema(self):
        from yt_playlist.core.migrations import migrate
        try:
            migrate(self.conn, backup_dir=self._backup_dir)
            if "settings" not in self.__dict__:
                self._bind_repositories()
            from yt_playlist.core import repair
            repair.run_once(self)
        except BaseException:
            self.conn.close()
            raise
