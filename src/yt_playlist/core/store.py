import sqlite3
import threading
from pathlib import Path

from yt_playlist.core.activity import interrupt_query
from yt_playlist.core.db_snapshot import read_snapshot
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.work_diagnostics import MaintenanceDiagnostics
from yt_playlist.core.priority import RequestPriority, ServingLock
# Store owns the connection, schema lifecycle, and explicitly named repositories.
from yt_playlist.repos.actions import ActionRepo
from yt_playlist.repos.artists import ArtistRepo
from yt_playlist.repos.base import synchronized as _synchronized
from yt_playlist.repos.charts import ChartsRepo
from yt_playlist.repos.collection import CollectionRepo
from yt_playlist.repos.cluster_evidence import ClusterEvidenceRepo
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
from yt_playlist.repos.rec_refresh import RecRefreshRepo
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
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, factory=DiagnosticConnection)
        self._db_path = None if str(db_path) == ":memory:" else Path(db_path).resolve()
        self._configure_connection()
        self.priority = RequestPriority()
        self._lock = ServingLock(self.priority)
        self.background_measurement = None
        self.maintenance_diagnostics = MaintenanceDiagnostics()
        self._backup_dir = backup_dir

    def _configure_connection(self):
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.set_progress_handler(interrupt_query, 1000)
        # Punctuation/space/accent-insensitive search key (see matching.search_squash): lets
        # seed_matches match 'LSD' against a title stored as 'L.S.D.' (#48).
        from yt_playlist.util.matching import search_squash
        self.conn.create_function("searchnorm", 1, search_squash, deterministic=True)

    def read_snapshot(self):
        """Read-only repository view for background calculation; never runs schema/setup work."""
        def make_reader():
            reader = Store.__new__(Store)
            reader._db_path = self._db_path
            path = self._db_path.as_uri() + "?mode=ro" if self._db_path else ":memory:"
            reader.conn = sqlite3.connect(path, uri=self._db_path is not None,
                                          check_same_thread=False, cached_statements=0,
                                          factory=DiagnosticConnection)
            reader._configure_connection()
            reader._lock = threading.RLock()
            reader._read_only = True
            reader.maintenance_diagnostics = self.maintenance_diagnostics
            reader._bind_repositories()
            return reader
        return read_snapshot(self, make_reader)

    def memoized_read(self, name, prepare):
        """Reuse a detached result on a pinned reader; name must include any non-database inputs."""
        calculate = getattr(self, "_memoized_read", None)
        return calculate(name, prepare) if calculate is not None else prepare()

    def _bind_repositories(self):
        # Each repository shares this connection and lock; consumers name its owner explicitly.
        self.overlaps = OverlapRepo(self)
        self.genres = GenreRepo(self)
        self.settings = SettingsRepo(self)
        self.actions = ActionRepo(self)
        self.artists = ArtistRepo(self)
        self.identities = IdentityRepo(self)
        self.history = HistoryRepo(self)
        self.collection = CollectionRepo(self)
        self.cluster_evidence = ClusterEvidenceRepo(self)
        self.rec_model = RecModelRepo(self)
        self.model_selection = ModelSelectionRepo(self)
        self.rec_query = RecQueryRepo(self)
        self.rec_surface = RecSurfaceRepo(self)
        self.rec_refresh = RecRefreshRepo(self)
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
            if self._db_path is not None:
                # A maintenance read transaction must not hold up UI writes (rollback journal
                # readers otherwise block the writer at commit, even on separate connections).
                mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode.lower() != "wal":
                    raise RuntimeError("Database requires WAL for nonblocking background readers")
            self._snapshot_revisions = Revisions(self.conn)
        except BaseException:
            self.conn.close()
            raise
