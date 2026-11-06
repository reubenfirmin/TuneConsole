import sqlite3
import threading
import uuid
from contextlib import contextmanager, ExitStack
from pathlib import Path

from yt_playlist.core.activity import interrupt_query
from yt_playlist.core.db_reads import MAX_READERS, ReaderDiagnostics, read_view
from yt_playlist.core.db_snapshot import read_snapshot
from yt_playlist.core.read_memo import ReadMemo
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core import evidence_ledger, file_owners
from yt_playlist.core.work_diagnostics import MaintenanceDiagnostics
from yt_playlist.core.priority import RequestPriority, ServingLock
from yt_playlist.core import collection_recovery, group_documents, metadata_delivery_files, metadata_runtime_files, policy_documents, recommendation_outputs, served_facts
# Store owns the connection, schema lifecycle, and explicitly named repositories.
from yt_playlist.repos.actions import ActionRepo
from yt_playlist.repos.album_descriptions import AlbumDescriptionsRepo
from yt_playlist.repos.audio_vibes import AudioVibesRepo
from yt_playlist.repos.artists import ArtistRepo
from yt_playlist.repos.base import synchronized as _synchronized
from yt_playlist.repos.charts import ChartsRepo
from yt_playlist.repos.collection import CollectionRepo
from yt_playlist.repos.cluster_evidence import ClusterEvidenceRepo
from yt_playlist.repos.enrichment import EnrichmentRepo
from yt_playlist.repos.enrichment_queue import EnrichmentQueueRepo
from yt_playlist.repos.enrichment_reports import EnrichmentReportsRepo
from yt_playlist.repos.enrichment_state import EnrichmentStateRepo
from yt_playlist.repos.genres import GenreRepo
from yt_playlist.repos.history import HistoryRepo
from yt_playlist.repos.accounts import AccountRepo
from yt_playlist.repos.modes import ModesRepo
from yt_playlist.repos.taste_groups import TasteGroupsRepo
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
from yt_playlist.repos.model_selection import ModelSelectionRepo
from yt_playlist.repos.overlaps import OverlapRepo
from yt_playlist.repos.playlists import PlaylistRepo
from yt_playlist.repos.player_events import PlayerEventsRepo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.rec_candidates import RecCandidatesRepo
from yt_playlist.repos.rec_eligibility import RecEligibilityRepo
from yt_playlist.repos.rec_evidence import RecEvidenceRepo
from yt_playlist.repos.rec_ledger import RecLedgerRepo
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.home_cache import HomeCacheRepo
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
from yt_playlist.repos.rec_signals import RecSignalsRepo
from yt_playlist.repos.playlist_recipes import PlaylistRecipesRepo
from yt_playlist.repos.artist_similarity import ArtistSimilarityRepo
from yt_playlist.repos.rec_refresh import RecRefreshRepo
from yt_playlist.repos.prepared_content import PreparedContentRepo
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.repos.vibe_predictions import VibePredictionsRepo
from yt_playlist.repos.road_trip import RoadTripRepo
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.search import SearchRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.repos.radio_checkpoints import RadioCheckpointRepo
from yt_playlist.repos.retention import RetentionRepo
from yt_playlist.repos.sync import SyncRepo
from yt_playlist.repos.readiness import ReadinessRepo
from yt_playlist.repos.radio_catalog import RadioCatalogRepo
from yt_playlist.repos.summary_inputs import SummaryInputsRepo
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo
from yt_playlist.repos.group_evidence import GroupEvidenceRepo
from yt_playlist.repos.album_feedback import AlbumFeedbackRepo
from yt_playlist.repos.playback_feedback import PlaybackFeedbackRepo
from yt_playlist.repos.tracks import TrackRepo
from yt_playlist.repos.trends import TrendsRepo
from yt_playlist.repos.tv_preparation import TVPreparationRepo
from yt_playlist.repos.wiki import WikiRepo

class Store:
    @property
    def activity_paused(self):
        source = getattr(self, '_evidence_source', self)
        return (getattr(source, '_activity_paused', False)
                or getattr(source, '_rebuild_source_version', None) == 64)

    @activity_paused.setter
    def activity_paused(self, value):
        self._activity_paused = value

    def __init__(self, db_path):
        # check_same_thread=False: FastAPI serves sync routes from a threadpool, so the connection
        # is touched from multiple threads. Access is serialized by self._lock (see _synchronized).
        if str(db_path) != ':memory:':
            from yt_playlist.core.db_rebuild.install import recover
            recover(Path(db_path).resolve())
        self.conn = sqlite3.connect(str(db_path), uri=True, check_same_thread=False, factory=DiagnosticConnection)
        self._db_path = None if str(db_path) == ":memory:" else Path(db_path).resolve()
        self._evidence_key = uuid.uuid4().hex
        from yt_playlist.core.playback_inbox import Inbox
        self._playback_feedback = Inbox(self._db_path, self._evidence_key)
        self.conn._policy_memory_key = self._evidence_key
        if self._db_path is None:
            import shutil
            runtime_root = metadata_runtime_files.root_for_database(None, self._evidence_key)
            self.conn._close_callbacks.append(lambda: shutil.rmtree(runtime_root, ignore_errors=True))
            from . import fao
            for domain in file_owners.owners():
                root = fao.root_for_database(None, self._evidence_key, domain)
                self.conn._close_callbacks.append(lambda root=root: shutil.rmtree(root, ignore_errors=True))
        self._configure_connection()
        self.priority = RequestPriority()
        self._lock = ServingLock(self.priority)
        self._evidence_state_lock = threading.Lock()
        self._analytics_readers = threading.BoundedSemaphore(MAX_READERS)
        self._background_readers = threading.BoundedSemaphore(MAX_READERS - 1)
        self._reader_diagnostics = ReaderDiagnostics()
        self._foreground_read_memo = ReadMemo()
        self.background_measurement = None
        self.transient_radio_playlists = set()
        self.maintenance_diagnostics = MaintenanceDiagnostics()

    def _configure_connection(self):
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.set_progress_handler(interrupt_query, 1000)
        # Punctuation/space/accent-insensitive search key (see matching.search_squash): lets
        # seed_matches match 'LSD' against a title stored as 'L.S.D.' (#48).
        from yt_playlist.util.matching import search_squash
        self.conn.create_function("searchnorm", 1, search_squash, deterministic=True)
        from yt_playlist.util.genre_catalog import normalize
        self.conn.create_function('tastenorm', 1, normalize, deterministic=True)

    @contextmanager
    def _retained_readers(self):
        """Keep every retained-file owner alive for a SQL reader's lifetime."""
        root = policy_documents.root_for_database(self._db_path, self._evidence_key)
        with ExitStack() as retained:
            retained.enter_context(file_owners.readers(self._db_path, self._evidence_key))
            retained.enter_context(policy_documents.retained_reader(root))
            retained.enter_context(policy_documents.retained_reader(
                evidence_ledger.root_for_database(self._db_path, self._evidence_key)))
            retained.enter_context(policy_documents.retained_reader(
                served_facts.root_for_database(self._db_path, self._evidence_key)))
            if getattr(self, '_collection_files', False):
                retained.enter_context(policy_documents.retained_reader(
                    collection_recovery.root_for_database(self._db_path,self._evidence_key)))
            if getattr(self, '_compact_group_files', False):
                retained.enter_context(policy_documents.retained_reader(
                    group_documents.root_for_database(self._db_path, self._evidence_key)))
            if getattr(self, '_recommendation_files', False):
                retained.enter_context(policy_documents.retained_reader(
                    recommendation_outputs.root_for_database(self._db_path, self._evidence_key)))
            if getattr(self, '_metadata_files', False):
                retained.enter_context(policy_documents.retained_reader(
                    metadata_delivery_files.root_for_database(self._db_path, self._evidence_key)))
                retained.enter_context(policy_documents.retained_reader(
                    metadata_runtime_files.root_for_database(self._db_path, self._evidence_key)))
            if getattr(self, '_legacy_files', False):
                from . import legacy_evidence
                retained.enter_context(policy_documents.retained_reader(
                    legacy_evidence.root_for_database(self._db_path, self._evidence_key)))
            yield

    @contextmanager
    def read_snapshot(self, *, dependencies=None):
        """Read-only background view, optionally restricted to declared revision keys."""
        with self._retained_readers():
            with read_snapshot(self, self._make_reader, dependencies=dependencies) as snapshot:
                yield snapshot

    @contextmanager
    def read_view(self, *, tracked=False, seconds=...):
        """Bounded private reader, optionally tracking reusable input reads."""
        if getattr(self, '_read_only', False):
            # A composed read keeps its original WAL snapshot and file lifetime.
            # Opening another reader here also repeats repository construction.
            yield self
            return
        with self._retained_readers():
            with read_view(self, self._make_reader, tracked=tracked, seconds=seconds) as reader:
                yield reader

    def _make_reader(self):
        reader = Store.__new__(Store)
        reader._db_path = self._db_path
        reader._evidence_key = self._evidence_key
        # Evidence recovery must inspect current serving roots and publish learning
        # through the owner, never through this read-only preparation snapshot.
        reader._evidence_source = getattr(self, '_evidence_source', self)
        reader.transient_radio_playlists = self.transient_radio_playlists
        reader._analytics_readers = self._analytics_readers
        path = self._db_path.as_uri() + "?mode=ro" if self._db_path else ":memory:"
        reader.conn = sqlite3.connect(path, uri=self._db_path is not None,
                                      check_same_thread=False, cached_statements=0,
                                      factory=DiagnosticConnection)
        reader.conn._policy_memory_key = self._evidence_key
        try:
            reader._configure_connection()
            reader._lock = threading.RLock()
            reader._read_only = True
            reader.maintenance_diagnostics = self.maintenance_diagnostics
            if reader._db_path is not None and getattr(reader._evidence_source,'_schema_compatibility',False):
                reader.conn.execute('BEGIN')
                reader.conn.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone()
            reader._bind_repositories()
            return reader
        except BaseException:
            reader.conn.close()
            raise

    def memoized_read(self, name, prepare):
        """Reuse a detached result on a pinned reader; name must include any non-database inputs."""
        calculate = getattr(self, "_memoized_read", None)
        return calculate(name, prepare) if calculate is not None else prepare()

    def _bind_repositories(self):
        # Each repository shares this connection and lock; consumers name its owner explicitly.
        if getattr(getattr(self,'_evidence_source',self),'_schema_compatibility',False):
            from . import entity_names
            entity_names.predecessor_reads(self.conn)
            if getattr(self, '_read_only', False):
                from .preparation import genre_publication
                genre_publication.install_reads(self.conn)
        self._repository_schema_version = self.conn.execute('PRAGMA schema_version').fetchone()[0]
        # sqlite_schema has no name index. Repeated probes each scan it and can
        # spend a background reader's entire deadline yielding to foreground work.
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='table'").fetchall()}
        self._collection_files = 'album_feedback' in tables
        self._recommendation_work = 'model_selection_schedule' in tables
        self._home_offer_dedup = 'home_offer_dedup' in tables
        self._album_note_demand = 'album_note_requests' in tables
        self._recommendation_requests = 'home_genre_requests' in tables
        self._listening_files = 'listening_output_publications' in tables
        self._home_display_files = 'home_display_publications' in tables
        self._choice_learning = 'choice_learning_publications' in tables
        self._recommendation_reports = 'recommendation_report_publications' in tables
        self._tv_recommendations = 'tv_continuation_receipts' in tables
        self._clicked_library_pools = 'clicked_library_pools' in tables
        self._model_comparison_state = 'model_comparison_plan' in tables
        self._model_projections = 'model_pair_heads' in tables
        self._paid_descriptions = 'album_description_requests' in tables
        self._compact_group_files = 'group_generations' in tables
        self._recommendation_files = 'recommendation_generations' in tables
        self._recommendation_revalidations = 'recommendation_pool_revalidations' in tables
        self._metadata_files = 'metadata_deliveries' in tables
        self._legacy_files = 'legacy_catalog_evidence' in tables
        self.overlaps = OverlapRepo(self)
        self.genres = GenreRepo(self)
        self.settings = SettingsRepo(self)
        self.radio_checkpoints = RadioCheckpointRepo(self)
        self.retention = RetentionRepo(self)
        self.sync = SyncRepo(self)
        self.readiness = ReadinessRepo(self)
        self.radio_catalog = RadioCatalogRepo(self)
        self.summary_inputs = SummaryInputsRepo(self)
        self.listening_analytics = ListeningAnalyticsRepo(self)
        self.group_evidence = GroupEvidenceRepo(self)
        self.album_feedback = AlbumFeedbackRepo(self)
        self.playback_feedback = PlaybackFeedbackRepo(self)
        self.actions = ActionRepo(self)
        self.album_descriptions = AlbumDescriptionsRepo(self)
        self.audio_vibes = AudioVibesRepo(self)
        self.artists = ArtistRepo(self)
        self.accounts = AccountRepo(self)
        self.history = HistoryRepo(self)
        self.collection = CollectionRepo(self)
        self.cluster_evidence = ClusterEvidenceRepo(self)
        self.rec_model = RecModelRepo(self)
        self.model_selection = ModelSelectionRepo(self)
        self.tv_preparation = TVPreparationRepo(self)
        self.rec_query = RecQueryRepo(self)
        self.rec_candidates = RecCandidatesRepo(self)
        self.rec_eligibility = RecEligibilityRepo(self)
        self.rec_evidence = RecEvidenceRepo(self)
        self.rec_ledger = RecLedgerRepo(self)
        self.rec_proposals = RecProposalsRepo(self)
        self.home_cache = HomeCacheRepo(self)
        self.recommendations = RecommendationPublicationsRepo(self)
        self.rec_signals = RecSignalsRepo(self)
        self.playlist_recipes = PlaylistRecipesRepo(self)
        self.artist_similarity = ArtistSimilarityRepo(self)
        self.rec_refresh = RecRefreshRepo(self)
        self.prepared_content = PreparedContentRepo(self)
        self.musical_profiles = MusicalProfilesRepo(self)
        self.vibe_predictions = VibePredictionsRepo(self)
        self.charts = ChartsRepo(self)
        self.tracks = TrackRepo(self)
        self.playlists = PlaylistRepo(self)
        self.player_events = PlayerEventsRepo(self)
        self.discovery = DiscoveryRepo(self)
        self.search = SearchRepo(self)
        self.enrichment = EnrichmentRepo(self)
        self.enrichment_queue = EnrichmentQueueRepo(self)
        self.enrichment_reports = EnrichmentReportsRepo(self)
        self.enrichment_state = EnrichmentStateRepo(self)
        self.wiki = WikiRepo(self)
        self.modes = ModesRepo(self)
        self.taste_groups = (CompactTasteGroupsRepo if self._compact_group_files else TasteGroupsRepo)(self)
        self.trends = TrendsRepo(self)
        self.road_trip = RoadTripRepo(self)

    @classmethod
    def open_current(cls, db_path):
        """Open an already constructed current database for domain preparation.

        Importers must finish and validate its schema first. This boundary runs
        neither migrations nor startup history repair on the private candidate.
        """
        from .migrations import current, regular
        store = cls(db_path)
        try:
            current.validate(store.conn)
            regular.validate(store.conn)
            store._schema_compatibility = False
            store._rebuild_source_version = None
            store._schema_recovery_pending = False
            store._bind_repositories()
            if store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise RuntimeError('Domain preparation requires WAL readers')
            store._snapshot_revisions = Revisions(store.conn)
            return store
        except BaseException:
            store.conn.close()
            raise

    @_synchronized
    def init_schema(self, *, defer_metadata=False):
        from yt_playlist.core.migrations import migrate
        try:
            self._schema_compatibility = bool(migrate(
                self.conn, defer_validation=defer_metadata and self._db_path is not None))
            self._rebuild_source_version = (64 if self.conn.execute(
                'SELECT max(version) FROM schema_migrations').fetchone()[0] == 64 else None)
            # Preparation changes no runtime owner. Old owners remain writable
            # until a separately verified, bounded publication replaces them.
            self._schema_recovery_pending = False
            if ("settings" not in self.__dict__ or self._repository_schema_version !=
                    self.conn.execute('PRAGMA schema_version').fetchone()[0]):
                self._bind_repositories()
            from yt_playlist.core import repair
            if not self._schema_compatibility:
                repair.run_once(self)
            if self._db_path is not None and not self._rebuild_source_version:
                # A maintenance read transaction must not hold up UI writes (rollback journal
                # readers otherwise block the writer at commit, even on separate connections).
                mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode.lower() != "wal":
                    raise RuntimeError("Database requires WAL for nonblocking background readers")
            self._snapshot_revisions = Revisions(self.conn)
            if self._rebuild_source_version:
                self.conn.execute('PRAGMA query_only=ON')
                self.conn._storage_compacting = True
        except BaseException:
            self.conn.close()
            raise
