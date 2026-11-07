from yt_playlist.core import file_paths
import sqlite3
import threading
import uuid
from contextlib import contextmanager, ExitStack
from pathlib import Path
from functools import partial

from yt_playlist.core.activity import interrupt_query
from yt_playlist.core.db_reads import MAX_READERS, ReaderDiagnostics, read_view
from yt_playlist.core.db_snapshot import read_snapshot
from yt_playlist.core.read_memo import ReadMemo
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core import evidence_ledger, file_owners
from yt_playlist.core.work_diagnostics import MaintenanceDiagnostics
from yt_playlist.controllers.wikipedia_cards import WikipediaCards
from yt_playlist.controllers.artist_similarity import ArtistSimilarity
from yt_playlist.controllers.player_events import PlayerEvents
from yt_playlist.controllers.rec_model import RecommendationModel
from yt_playlist.controllers.model_selection import ModelSelection
from yt_playlist.controllers.playlist_recipes import PlaylistRecipes
from yt_playlist.core.playlist_files import PlaylistFiles
from yt_playlist.controllers.embeddings import Embeddings
from yt_playlist.repos.embeddings import EmbeddingRepo
from yt_playlist.core.embedding_files import EmbeddingFiles
from yt_playlist.controllers.search import Search
from yt_playlist.controllers.recommendation_refresh import RecommendationRefresh
from yt_playlist.controllers.recommendation_candidates import RecommendationCandidates
from yt_playlist.controllers.recommendation_eligibility import RecommendationEligibility
from yt_playlist.controllers.recommendation_evidence import RecommendationEvidence
from yt_playlist.controllers.recommendation_query import RecommendationQuery
from yt_playlist.controllers.recap import Recap
from yt_playlist.controllers.trends import Trends
from yt_playlist.controllers.rec_signals import RecommendationSignals
from yt_playlist.controllers.summary_inputs import SummaryInputs
from yt_playlist.controllers.sync import LibrarySync
from yt_playlist.controllers.charts import Charts
from yt_playlist.controllers.readiness import Readiness
from yt_playlist.controllers.cluster_evidence import ClusterEvidence
from yt_playlist.controllers.enrichment_queue import EnrichmentQueue
from yt_playlist.controllers.enrichment_reports import EnrichmentReports
from yt_playlist.controllers.album_descriptions import AlbumDescriptions
from yt_playlist.controllers.album_feedback import AlbumFeedback
from yt_playlist.controllers.listening_analytics import ListeningAnalytics
from yt_playlist.controllers.artists import Artists
from yt_playlist.controllers.collection import Collection
from yt_playlist.controllers.discovery import Discovery
from yt_playlist.controllers.tracks import Tracks
from yt_playlist.controllers.metadata_choices import MetadataChoices
from yt_playlist.controllers.metadata_deliveries import MetadataDeliveries
from yt_playlist.repos.metadata_deliveries import MetadataDeliveryRepo
from yt_playlist.core.metadata_delivery_files import MetadataDeliveryFiles, root_for_database as metadata_delivery_root
from yt_playlist.repos.metadata_choices import MetadataChoiceRepo
from yt_playlist.core.metadata_runtime_files import RuntimeMetadataFiles, root_for_database as metadata_runtime_root
from yt_playlist.controllers.playback_feedback import PlaybackFeedback
from yt_playlist.controllers.recommendation_ledger import RecommendationLedger
from yt_playlist.controllers.playback_contexts import PlaybackContexts
from yt_playlist.controllers.recommendation_impressions import RecommendationImpressions
from yt_playlist.core.served_files import ServedFiles
from yt_playlist.controllers.policies import Policies
from yt_playlist.repos.policies import PolicyRepo
from yt_playlist.core.policy_files import PolicyFiles
from yt_playlist.core.priority import RequestPriority, ServingLock
from yt_playlist.core import album_description_files, album_feedback_outbox, collection_recovery, group_documents, metadata_delivery_files, metadata_runtime_files, policy_documents, recommendation_outputs, served_facts
# Store owns the connection, schema lifecycle, and explicitly named repositories.
from yt_playlist.repos.actions import ActionRepo
from yt_playlist.repos.album_descriptions import AlbumDescriptionsRepo
from yt_playlist.repos.audio_vibes import AudioVibesRepo
from yt_playlist.controllers.audio_vibes import AudioVibes
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
from yt_playlist.controllers.genres import Genres
from yt_playlist.repos.history import HistoryRepo
from yt_playlist.repos.accounts import AccountRepo
from yt_playlist.controllers.accounts import Accounts
from yt_playlist.repos.modes import ModesRepo
from yt_playlist.repos.taste_groups import TasteGroupsRepo
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
from yt_playlist.repos.model_selection import ModelSelectionRepo
from yt_playlist.repos.overlaps import OverlapRepo
from yt_playlist.repos.playlists import PlaylistRepo
from yt_playlist.controllers.playlists import Playlists
from yt_playlist.repos.player_events import PlayerEventsRepo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.recap import RecapRepo
from yt_playlist.repos.rec_candidates import RecCandidatesRepo
from yt_playlist.repos.rec_eligibility import RecEligibilityRepo
from yt_playlist.repos.rec_evidence import RecEvidenceRepo
from yt_playlist.repos.rec_ledger import RecLedgerRepo
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.home_cache import HomeCacheRepo
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.repos.rec_signals import RecSignalsRepo
from yt_playlist.repos.playlist_recipes import PlaylistRecipesRepo
from yt_playlist.repos.artist_similarity import ArtistSimilarityRepo
from yt_playlist.repos.rec_refresh import RecRefreshRepo
from yt_playlist.repos.prepared_content import PreparedContentRepo
from yt_playlist.controllers.prepared_content import PreparedContent
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo
from yt_playlist.repos.vibe_predictions import VibePredictionsRepo
from yt_playlist.repos.road_trip import RoadTripRepo
from yt_playlist.controllers.road_trip import RoadTrips
from yt_playlist.repos.discovery import DiscoveryRepo
from yt_playlist.repos.search import SearchRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.repos.radio_checkpoints import RadioCheckpointRepo
from yt_playlist.controllers.radio_checkpoints import RadioCheckpoints
from yt_playlist.core.radio_files import RadioFiles
from yt_playlist.controllers.retention import Retention
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
                or getattr(source, '_recommendation_retirement_pending', False)
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
                root = file_paths.root_for_database(None, self._evidence_key, domain)
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
        """Bounded reporting reader without a transaction; optionally reuse tracked results."""
        if getattr(self, '_read_only', False):
            # A composed read keeps its existing connection and file lifetime.
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
            # Memory readers bind after their private SQL copy is populated.
            # Binding the empty database here repeats every owner construction.
            if self._db_path is not None:
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
        from yt_playlist.controllers.retained_originals import RetainedOriginals
        from yt_playlist.repos.retained_originals import RetainedOriginalsRepo
        from yt_playlist.core.original_files import OriginalFiles
        from yt_playlist.core.legacy_evidence import root_for_database as original_root
        self.originals = RetainedOriginals(RetainedOriginalsRepo(self), OriginalFiles(
            file_paths.root_for_database(self._db_path, self._evidence_key, 'legacy-journals'),
            original_root(self._db_path, self._evidence_key)))
        self.overlaps = OverlapRepo(self)
        self.settings = SettingsRepo(self)
        self.metadata_deliveries = MetadataDeliveries(MetadataDeliveryRepo(self),
            MetadataDeliveryFiles(metadata_delivery_root(self._db_path, self._evidence_key)))
        self.metadata_choices = MetadataChoices(MetadataChoiceRepo(self),
            RuntimeMetadataFiles(metadata_runtime_root(self._db_path, self._evidence_key)), self.metadata_deliveries)
        from yt_playlist.controllers.artist_state import ArtistState
        from yt_playlist.repos.artist_state import ArtistStateRepo
        self.artist_state = ArtistState(ArtistStateRepo(self), self.originals)
        self.artists = Artists(ArtistRepo(self), self.artist_state,
            lambda previous, target, **options: self.discovery.reconcile_artist(previous, target, **options), self.originals)
        self.rec_eligibility = RecommendationEligibility(RecEligibilityRepo(self), self.settings)
        from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
        from yt_playlist.repos.recommendation_reports import RecommendationReportsRepo
        from yt_playlist.repos.album_notes import AlbumNotesRepo
        from yt_playlist.core.preparation.listening_outputs import read_original as read_listening
        recommendation_files = RecommendationFiles(recommendation_outputs.root_for_database(self._db_path, self._evidence_key))
        self.listening_analytics = ListeningAnalytics(ListeningAnalyticsRepo(self), recommendation_files.publish,
            recommendation_outputs.read, read_listening, recommendation_files.exists)
        from yt_playlist.controllers.group_evidence import GroupEvidence
        from yt_playlist.core.group_evidence_files import GroupEvidenceFiles
        from yt_playlist.core import group_admission, home_outputs
        self.group_evidence = GroupEvidence(GroupEvidenceRepo(self), lambda: self.rec_ledger.evidence(),
            partial(group_admission.admission, self), GroupEvidenceFiles(
                group_documents.root_for_database(self._db_path, self._evidence_key), recommendation_files.root),
            lambda: self._lock, enabled=self._compact_group_files, recommendations=self._recommendation_files)
        from yt_playlist.controllers.home_outputs import HomeOutputs
        self.home_cache = HomeOutputs(HomeCacheRepo(self), self.group_evidence, recommendation_files,
                                      lambda *args: home_outputs.read_manifest(*args))
        self.rec_proposals = RecommendationProposals(RecProposalsRepo(self), RecommendationReportsRepo(self),
            AlbumNotesRepo(self), recommendation_files, self.listening_analytics, self.home_cache,
            lambda: self.recommendations)
        from yt_playlist.controllers.choice_findings import ChoiceFindings
        self.choice_findings = ChoiceFindings(self.rec_proposals)
        from yt_playlist.controllers.album_notes import AlbumNotes
        self.album_notes = AlbumNotes(AlbumNotesRepo(self), self.rec_proposals, RecommendationFiles(
            recommendation_outputs.root_for_database(self._db_path, self._evidence_key)),
            installed=self._album_note_demand,
            paused=lambda: self.activity_paused or getattr(self, '_read_only', False))
        self.genres = Genres(GenreRepo(self), lambda previous, target: self.discovery.merge_genre_origins(previous, target))
        from yt_playlist.controllers.albums import Albums
        from yt_playlist.repos.albums import AlbumRepo
        self.albums = Albums(AlbumRepo(self), self.artists, self.genres)
        self.playlist_recipes = PlaylistRecipes(PlaylistRecipesRepo(self), PlaylistFiles(
            collection_recovery.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock)
        from yt_playlist.core.preparation.road_trip_drafts import read_original_parts
        self.road_trip = RoadTrips(RoadTripRepo(self),
            collection_recovery.RecoveryFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)),
            read_original_parts)
        from yt_playlist.controllers.recording_keys import RecordingKeys
        from yt_playlist.repos.recording_keys import RecordingKeysRepo
        from yt_playlist.controllers.listening_calendar import ListeningCalendar
        from yt_playlist.repos.listening_calendar import ListeningCalendarRepo
        self.listening_calendar = ListeningCalendar(ListeningCalendarRepo(self))
        self.recording_keys = RecordingKeys(RecordingKeysRepo(self), self.originals, self.playlist_recipes,
            self.road_trip, self.listening_calendar)
        tracks = TrackRepo(self)
        self.tracks = Tracks(tracks, self.metadata_choices, self.artists, self.albums, self.recording_keys,
            lambda *args, **kwargs: self.recording_associations.associate(*args, **kwargs))
        self.discovery = Discovery(DiscoveryRepo(self), self.artists, self.rec_eligibility,
            collection_recovery.RecoveryFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)),
            originals=self.originals, tracks=self.tracks, albums=self.albums, genres=self.genres,
            metadata=self.metadata_choices, keys=self.recording_keys, proposals=self.rec_proposals, settings=self.settings)
        self.recap = Recap(RecapRepo(self), self.genres)
        self.radio_checkpoints = RadioCheckpoints(RadioCheckpointRepo(self), self.settings, self.tracks,
            RadioFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock)
        self.retention = Retention(self)
        from yt_playlist.core.published_documents import read_original
        self.sync = LibrarySync(SyncRepo(self), self.settings,
            collection_recovery.RecoveryFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)),
            read_original)
        self.radio_catalog = RadioCatalogRepo(self)
        self.summary_inputs = SummaryInputs(SummaryInputsRepo(self), self.settings)
        self.playback_feedback = PlaybackFeedback(PlaybackFeedbackRepo(self),
            getattr(self, '_evidence_source', self)._playback_feedback, self)
        from yt_playlist.controllers.actions import Actions
        from yt_playlist.core.action_backup_files import ActionBackupFiles
        action_root = collection_recovery.root_for_database(self._db_path, self._evidence_key)
        self.actions = Actions(ActionRepo(self), collection_recovery.RecoveryFiles(action_root), ActionBackupFiles(action_root),
            str(self._db_path) if self._db_path else f'memory-{id(self.conn)}', lambda: self._lock)
        self.accounts = Accounts(AccountRepo(self), self.settings,
            paused=lambda: self.activity_paused,
            maintenance_accounts=lambda: getattr(self, '_maintenance_accounts', None))
        self.collection = Collection(CollectionRepo(self), self.albums)
        self.album_feedback = AlbumFeedback(AlbumFeedbackRepo(self), self.collection,
            partial(album_feedback_outbox.fence, self), read_original)
        self.cluster_evidence = ClusterEvidence(ClusterEvidenceRepo(self))
        self.player_events = PlayerEvents(PlayerEventsRepo(self))
        self.embeddings = Embeddings(EmbeddingRepo(self), EmbeddingFiles(
            file_paths.root_for_database(self._db_path, self._evidence_key, 'embeddings')))
        self.rec_model = RecommendationModel(RecModelRepo(self), self.settings, self.player_events,
            self.genres, self.embeddings, collection_recovery.RecoveryFiles(
                collection_recovery.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock)
        from yt_playlist.controllers.recording_associations import RecordingAssociations
        self.recording_associations = RecordingAssociations(tracks, self.metadata_choices, self.recording_keys,
            self.discovery, self.listening_calendar, self.rec_model)
        from yt_playlist.controllers.tv_preparation import TVPreparation
        self.tv_preparation = TVPreparation(TVPreparationRepo(self), self.rec_proposals)
        from yt_playlist.controllers.tv_state import TVState
        from yt_playlist.repos.tv_state import TVStateRepo
        self.tv = TVState(TVStateRepo(self), self.rec_proposals, installed=self._tv_recommendations)
        self.rec_query = RecommendationQuery(RecQueryRepo(self), self.genres, self.rec_eligibility, self.artists)
        self.readiness = Readiness(ReadinessRepo(self), self.rec_eligibility)
        self.rec_candidates = RecommendationCandidates(RecCandidatesRepo(self), self.rec_eligibility)
        self.rec_evidence = RecommendationEvidence(RecEvidenceRepo(self), self.rec_eligibility, self.genres,
            read_view=self.read_view, read_only=getattr(self, '_read_only', False))
        policies = Policies(PolicyRepo(self), PolicyFiles(
            policy_documents.root_for_database(self._db_path, self._evidence_key),
            file_paths.root_for_database(self._db_path, self._evidence_key, 'policy-history')), lambda: self._lock)
        ledger_repository = RecLedgerRepo(self)
        self.rec_ledger = RecommendationLedger(ledger_repository, policies, lambda: self._lock,
            playback=PlaybackContexts(ledger_repository, self.settings, self.tracks, self.collection, lambda: self._lock),
            impressions=RecommendationImpressions(ledger_repository, ServedFiles(
                served_facts.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock),
            ledger_factory=partial(evidence_ledger.EvidenceLedger,
                evidence_ledger.root_for_database(self._db_path, self._evidence_key)))
        self.model_selection = ModelSelection(ModelSelectionRepo(self), self.settings, self.rec_proposals,
            self.radio_checkpoints, self.embeddings, lambda: self._lock)
        self.album_descriptions = AlbumDescriptions(AlbumDescriptionsRepo(self), album_description_files.read,
            partial(album_description_files.publish,
                    album_description_files.root_for_database(self._db_path, self._evidence_key)),
            partial(policy_documents.publish, policy_documents.root_for_database(self._db_path, self._evidence_key)),
            proposals=self.rec_proposals, installed=self._paid_descriptions)
        self.rec_signals = RecommendationSignals(RecSignalsRepo(self))
        self.artist_similarity = ArtistSimilarity(ArtistSimilarityRepo(self), self.artists)
        self.rec_refresh = RecommendationRefresh(RecRefreshRepo(self), self.settings)
        self.prepared_content = PreparedContent(PreparedContentRepo(self), self.discovery, self.rec_refresh)
        from yt_playlist.controllers.vibe_predictions import VibePredictions
        from yt_playlist.controllers.vibe_models import VibeModels
        from yt_playlist.repos.vibe_models import VibeModelsRepo
        from yt_playlist.controllers.musical_profiles import MusicalProfiles
        from yt_playlist.core import (profile_files, metadata_delivery_files, metadata_runtime_files,
                                     sample_files, vibe_model_files, vibe_provenance)
        from yt_playlist.core.audio_metadata_files import AudioMetadataFiles
        from yt_playlist.core.preparation import metadata_destinations
        metadata_files = AudioMetadataFiles(metadata_runtime_files.root_for_database(self._db_path, self._evidence_key),
            metadata_delivery_files.root_for_database(self._db_path, self._evidence_key),
            metadata_destinations.directory(self.conn))
        provenance_files = vibe_provenance.ProvenanceFiles(vibe_provenance.directory(self.conn))
        self.vibe_models = VibeModels(VibeModelsRepo(self), vibe_model_files.VibeModelFiles(
            file_paths.root_for_database(self._db_path, self._evidence_key, 'vibe-models')), provenance_files)
        self.vibe_predictions = VibePredictions(VibePredictionsRepo(self), provenance_files,
            sample_files.SampleFiles(sample_files.root_for_database(self._db_path, self._evidence_key)),
            metadata_files, self.vibe_models,
            self.settings, self.rec_refresh, self.prepared_content, self.genres)
        self.musical_profiles = MusicalProfiles(MusicalProfilesRepo(self),
            profile_files.ProfileFiles(file_paths.root_for_database(self._db_path, self._evidence_key, 'profiles')),
            metadata_files, self.vibe_predictions, self.rec_refresh)
        self.charts = Charts(ChartsRepo(self))
        self.playlists = Playlists(PlaylistRepo(self), self.tracks, self.radio_checkpoints, lambda: self._lock)
        from yt_playlist.core import audio_annotation_files
        self.audio_vibes = AudioVibes(AudioVibesRepo(self),
            sample_files.SampleFiles(sample_files.root_for_database(self._db_path, self._evidence_key)),
            audio_annotation_files.AnnotationFiles(
                audio_annotation_files.root_for_database(self._db_path, self._evidence_key)),
            self.prepared_content, AudioMetadataFiles(
                metadata_runtime_files.root_for_database(self._db_path, self._evidence_key),
                metadata_delivery_files.root_for_database(self._db_path, self._evidence_key)))
        self.search = Search(SearchRepo(self))
        from yt_playlist.controllers.enrichment import Enrichment
        self.enrichment = Enrichment(EnrichmentRepo(self), self.metadata_choices,
            RuntimeMetadataFiles(metadata_runtime_root(self._db_path, self._evidence_key)))
        self.enrichment_reports = EnrichmentReports(EnrichmentReportsRepo(self),
            partial(metadata_runtime_files.read_at,
                    metadata_runtime_files.root_for_database(self._db_path, self._evidence_key)))
        from yt_playlist.controllers.enrichment_state import EnrichmentState
        from yt_playlist.providers.enrichment import load_config
        self.enrichment_state = EnrichmentState(EnrichmentStateRepo(self), self.metadata_choices,
            RuntimeMetadataFiles(metadata_runtime_root(self._db_path, self._evidence_key)),
            MetadataDeliveryFiles(metadata_delivery_root(self._db_path, self._evidence_key)), self.metadata_deliveries,
            self.enrichment, lambda: [provider['name'] for provider in load_config(self) if provider['enabled']],
            self.originals.files)
        self.conn._close_callbacks.append(self.enrichment_state.files.close)
        self.enrichment_queue = EnrichmentQueue(EnrichmentQueueRepo(self), self.enrichment_state, self.settings,
            read_snapshot=self.read_snapshot, memoized_read=self.memoized_read,
            read_only=getattr(self, '_read_only', False))
        self.wiki = WikipediaCards(WikiRepo(self))
        self.modes = ModesRepo(self)
        self.recommendations = RecommendationPublications(RecommendationPublicationsRepo(self), self.rec_query,
            self.discovery, self.modes, RecommendationFiles(
                recommendation_outputs.root_for_database(self._db_path, self._evidence_key)))
        if self._compact_group_files:
            from yt_playlist.controllers.compact_taste_groups import CompactTasteGroups
            from yt_playlist.controllers.group_retention import GroupRetention
            from yt_playlist.repos.group_retention_queries import GroupRetentionRepo
            from yt_playlist.core import profile_files
            self.group_retention = GroupRetentionRepo(self)
            self.taste_groups = CompactTasteGroups(CompactTasteGroupsRepo(self), group_documents.GroupFiles(
                group_documents.root_for_database(self._db_path, self._evidence_key),
                policy_documents.root_for_database(self._db_path, self._evidence_key)),
                profile_files.ProfileFiles(file_paths.root_for_database(self._db_path, self._evidence_key, 'profiles')),
                GroupRetention(self.group_retention, self.read_snapshot))
        else:
            from yt_playlist.controllers.taste_groups import TasteGroups
            from yt_playlist.core import profile_files
            self.taste_groups = TasteGroups(TasteGroupsRepo(self),
                profile_files.ProfileFiles(file_paths.root_for_database(self._db_path, self._evidence_key, 'profiles')))
        self.trends = Trends(TrendsRepo(self), self.rec_proposals)
        from yt_playlist.controllers.history import History
        from yt_playlist.controllers.history_maintenance import HistoryMaintenance
        from yt_playlist.core.history_files import HistoryRepairFiles
        history = HistoryRepo(self)
        self.history = History(history, self.artists, HistoryMaintenance(
            history, self.settings, self.discovery, self.embeddings, self.model_selection,
            self.rec_proposals, HistoryRepairFiles(), lambda: self._lock, self.listening_calendar),
            lambda: self.transient_radio_playlists, lambda: self._lock, self.listening_calendar)

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
