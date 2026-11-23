"""Application construction for a connection, separate from its SQLite lifecycle.

The session is the composition root's target. Controllers receive only their named
repositories, file owners and callbacks; rebinding reconstructs all of these together.
"""
from yt_playlist.core import file_paths
from functools import partial

from yt_playlist.core import evidence_ledger
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
from yt_playlist.core import album_feedback_outbox, collection_recovery, group_documents, metadata_delivery_files, metadata_runtime_files, policy_documents, recommendation_outputs, served_facts
from yt_playlist.repos.actions import ActionRepo
from yt_playlist.repos.album_descriptions import AlbumDescriptionsRepo
from yt_playlist.repos.audio_vibes import AudioVibesRepo
from yt_playlist.controllers.audio_vibes import AudioVibes
from yt_playlist.repos.artists import ArtistRepo
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

def bind(self):
        """Construct the application owners against one supplied database session."""
        # Each repository shares this connection and lock; consumers name its owner explicitly.
        # Application admission completes bounded migrations before binding.
        self.overlaps = OverlapRepo(self)
        from yt_playlist.controllers.settings import Settings
        recommendation_files = RecommendationFiles(recommendation_outputs.root_for_database(self._db_path, self._evidence_key))
        self.settings = Settings(SettingsRepo(self), recommendation_files, collection_recovery.RecoveryFiles(
            collection_recovery.root_for_database(self._db_path, self._evidence_key)))
        self.metadata_deliveries = MetadataDeliveries(MetadataDeliveryRepo(self),
            MetadataDeliveryFiles(metadata_delivery_root(self._db_path, self._evidence_key)))
        self.metadata_choices = MetadataChoices(MetadataChoiceRepo(self),
            RuntimeMetadataFiles(metadata_runtime_root(self._db_path, self._evidence_key)), self.metadata_deliveries)
        from yt_playlist.controllers.artist_state import ArtistState
        from yt_playlist.repos.artist_state import ArtistStateRepo
        self.artist_state = ArtistState(ArtistStateRepo(self))
        self.artists = Artists(ArtistRepo(self), self.artist_state,
            lambda previous, target, **options: self.discovery.reconcile_artist(previous, target, **options))
        self.rec_eligibility = RecommendationEligibility(RecEligibilityRepo(self), self.settings)
        from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
        from yt_playlist.repos.recommendation_reports import RecommendationReportsRepo
        from yt_playlist.repos.album_notes import AlbumNotesRepo
        self.listening_analytics = ListeningAnalytics(ListeningAnalyticsRepo(self), recommendation_files,
            recommendation_outputs.read, recommendation_files.exists)
        from yt_playlist.controllers.group_evidence import GroupEvidence
        from yt_playlist.core.group_evidence_files import GroupEvidenceFiles
        from yt_playlist.core import group_admission, home_outputs
        self.group_evidence = GroupEvidence(GroupEvidenceRepo(self), lambda: self.rec_ledger.evidence(),
            partial(group_admission.admission, self), GroupEvidenceFiles(
                group_documents.root_for_database(self._db_path, self._evidence_key), recommendation_files.root),
            lambda: self._lock)
        from yt_playlist.controllers.home_outputs import HomeOutputs
        self.home_cache = HomeOutputs(HomeCacheRepo(self), self.group_evidence, recommendation_files,
                                      lambda *args: home_outputs.read_manifest(*args))
        self.rec_proposals = RecommendationProposals(RecProposalsRepo(self), RecommendationReportsRepo(self),
            AlbumNotesRepo(self), recommendation_files, self.listening_analytics, self.home_cache,
            lambda: self.recommendations)
        from yt_playlist.controllers.choice_findings import ChoiceFindings
        self.choice_findings = ChoiceFindings(self.rec_proposals)
        from yt_playlist.controllers.album_notes import AlbumNotes
        self.album_notes = AlbumNotes(AlbumNotesRepo(self), self.rec_proposals,
            paused=lambda: self.activity_paused or getattr(self, '_read_only', False))
        self.genres = Genres(GenreRepo(self))
        from yt_playlist.controllers.albums import Albums
        from yt_playlist.repos.albums import AlbumRepo
        self.albums = Albums(AlbumRepo(self), self.artists, self.genres)
        self.playlist_recipes = PlaylistRecipes(PlaylistRecipesRepo(self), PlaylistFiles(
            collection_recovery.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock)
        self.road_trip = RoadTrips(RoadTripRepo(self),
            collection_recovery.RecoveryFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)))
        from yt_playlist.controllers.recording_keys import RecordingKeys
        from yt_playlist.repos.recording_keys import RecordingKeysRepo
        from yt_playlist.controllers.listening_calendar import ListeningCalendar
        from yt_playlist.repos.listening_calendar import ListeningCalendarRepo
        self.listening_calendar = ListeningCalendar(ListeningCalendarRepo(self))
        self.recording_keys = RecordingKeys(RecordingKeysRepo(self), self.playlist_recipes,
            self.road_trip, self.listening_calendar)
        tracks = TrackRepo(self)
        self.tracks = Tracks(tracks, self.metadata_choices, self.artists, self.albums, self.recording_keys,
            lambda *args, **kwargs: self.recording_associations.associate(*args, **kwargs))
        self.discovery = Discovery(DiscoveryRepo(self), self.artists, self.rec_eligibility,
            collection_recovery.RecoveryFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)),
            tracks=self.tracks, albums=self.albums, genres=self.genres,
            metadata=self.metadata_choices, keys=self.recording_keys, proposals=self.rec_proposals)
        self.recap = Recap(RecapRepo(self), self.genres)
        self.radio_checkpoints = RadioCheckpoints(RadioCheckpointRepo(self), self.tracks,
            RadioFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock)
        self.sync = LibrarySync(SyncRepo(self),
            collection_recovery.RecoveryFiles(collection_recovery.root_for_database(self._db_path, self._evidence_key)))
        self.radio_catalog = RadioCatalogRepo(self)
        self.summary_inputs = SummaryInputs(SummaryInputsRepo(self), self.settings)
        self.playback_feedback = PlaybackFeedback(PlaybackFeedbackRepo(self),
            getattr(self, '_evidence_source', self)._playback_feedback, self)
        from yt_playlist.controllers.actions import Actions
        from yt_playlist.core.action_backup_files import ActionBackupFiles
        action_root = collection_recovery.root_for_database(self._db_path, self._evidence_key)
        self.actions = Actions(ActionRepo(self), collection_recovery.RecoveryFiles(action_root), ActionBackupFiles(action_root),
            str(self._db_path) if self._db_path else f'memory-{id(self.conn)}', lambda: self._lock)
        self.accounts = Accounts(AccountRepo(self), paused=lambda: self.activity_paused)
        from yt_playlist.controllers.album_aliases import AlbumAliases
        from yt_playlist.repos.album_aliases import AlbumAliasesRepo
        aliases = AlbumAliases(AlbumAliasesRepo(self), self.albums, self.discovery,
            lambda target, retired, now: self.album_feedback.merge_albums(target, retired, now), lambda: self._lock)
        self.collection = Collection(CollectionRepo(self), self.albums, aliases)
        self.album_feedback = AlbumFeedback(AlbumFeedbackRepo(self), partial(album_feedback_outbox.fence, self))
        self.cluster_evidence = ClusterEvidence(ClusterEvidenceRepo(self))
        self.player_events = PlayerEvents(PlayerEventsRepo(self))
        self.embeddings = Embeddings(EmbeddingRepo(self), EmbeddingFiles(
            file_paths.root_for_database(self._db_path, self._evidence_key, 'embeddings')))
        from yt_playlist.library.listening_window import active_session, first_steering_session
        from yt_playlist.core.rec_model_policy import PERMANENT_SURFACE
        self.rec_model = RecommendationModel(RecModelRepo(self), self.settings, self.player_events,
            self.genres, self.embeddings, collection_recovery.RecoveryFiles(
                collection_recovery.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock,
            active_session=lambda now: active_session(self, now),
            first_session=lambda at, until: first_steering_session(self, at, until),
            permanent=lambda: self.memoized_read('listener-permanent-weights',
                lambda: self.rec_proposals.proposal_fields(PERMANENT_SURFACE, ('weights',))))
        from yt_playlist.controllers.recording_associations import RecordingAssociations
        self.recording_associations = RecordingAssociations(tracks, self.metadata_choices, self.recording_keys,
            self.discovery, self.listening_calendar, self.rec_model)
        from yt_playlist.controllers.tv_preparation import TVPreparation
        self.tv_preparation = TVPreparation(TVPreparationRepo(self))
        from yt_playlist.controllers.tv_state import TVState
        from yt_playlist.repos.tv_state import TVStateRepo
        self.tv = TVState(TVStateRepo(self))
        self.rec_query = RecommendationQuery(RecQueryRepo(self), self.genres, self.rec_eligibility, self.artists)
        self.readiness = Readiness(ReadinessRepo(self), self.rec_eligibility)
        self.rec_candidates = RecommendationCandidates(RecCandidatesRepo(self), self.rec_eligibility)
        self.rec_evidence = RecommendationEvidence(RecEvidenceRepo(self), self.rec_eligibility, self.genres,
            read_view=self.read_view, read_only=getattr(self, '_read_only', False))
        policies = Policies(PolicyRepo(self), PolicyFiles(
            policy_documents.root_for_database(self._db_path, self._evidence_key),
            file_paths.root_for_database(self._db_path, self._evidence_key, 'policy-history')), lambda: self._lock)
        from yt_playlist.controllers.evidence_ledger import EvidenceLedger
        from yt_playlist.repos.evidence_files import EvidenceFileRepo
        self.evidence_files = EvidenceFileRepo(getattr(self, '_evidence_source', self))
        ledger_repository = RecLedgerRepo(self)
        self.rec_ledger = RecommendationLedger(ledger_repository, policies, lambda: self._lock,
            playback=PlaybackContexts(ledger_repository, self.tracks, self.collection, lambda: self._lock),
            impressions=RecommendationImpressions(ledger_repository, ServedFiles(
                served_facts.root_for_database(self._db_path, self._evidence_key)), lambda: self._lock),
            ledger_factory=partial(EvidenceLedger, self.evidence_files,
                evidence_ledger.root_for_database(self._db_path, self._evidence_key)))
        self.model_selection = ModelSelection(ModelSelectionRepo(self), self.settings,
            self.embeddings, self.rec_model, lambda: self._lock)
        self.album_descriptions = AlbumDescriptions(AlbumDescriptionsRepo(self), policies)
        self.rec_signals = RecommendationSignals(RecSignalsRepo(self))
        self.artist_similarity = ArtistSimilarity(ArtistSimilarityRepo(self), self.artists)
        self.rec_refresh = RecommendationRefresh(RecRefreshRepo(self))
        self.prepared_content = PreparedContent(PreparedContentRepo(self), self.discovery, self.rec_refresh)
        from yt_playlist.controllers.vibe_predictions import VibePredictions
        from yt_playlist.controllers.vibe_models import VibeModels
        from yt_playlist.repos.vibe_models import VibeModelsRepo
        from yt_playlist.controllers.musical_profiles import MusicalProfiles
        from yt_playlist.core import (profile_files, metadata_delivery_files, metadata_runtime_files,
                                     sample_files, vibe_model_files, vibe_provenance)
        from yt_playlist.core.audio_metadata_files import AudioMetadataFiles
        metadata_files = AudioMetadataFiles(metadata_runtime_files.root_for_database(self._db_path, self._evidence_key),
            metadata_delivery_files.root_for_database(self._db_path, self._evidence_key))
        provenance_files = vibe_provenance.ProvenanceFiles(vibe_provenance.directory(self.conn))
        self.vibe_models = VibeModels(VibeModelsRepo(self), vibe_model_files.VibeModelFiles(
            file_paths.root_for_database(self._db_path, self._evidence_key, 'vibe-models')), provenance_files,
            file_repository=VibeModelsRepo(getattr(self, '_evidence_source', self)))
        self.vibe_predictions = VibePredictions(VibePredictionsRepo(self), provenance_files,
            sample_files.SampleFiles(sample_files.root_for_database(self._db_path, self._evidence_key)),
            metadata_files, self.vibe_models,
            self.rec_refresh, self.prepared_content, self.genres,
            file_repository=VibePredictionsRepo(getattr(self, '_evidence_source', self)))
        self.musical_profiles = MusicalProfiles(MusicalProfilesRepo(self),
            profile_files.ProfileFiles(file_paths.root_for_database(self._db_path, self._evidence_key, 'profiles')),
            metadata_files, self.vibe_predictions, self.rec_refresh)
        self.charts = Charts(ChartsRepo(self))
        self.playlists = Playlists(PlaylistRepo(self), self.tracks, self.radio_checkpoints, lambda: self._lock)
        from yt_playlist.controllers.playlist_backups import PlaylistBackups
        from yt_playlist.repos.playlist_backups import PlaylistBackupsRepo
        from yt_playlist.core.playlist_backup_files import PlaylistBackupFiles
        self.playlist_backups = PlaylistBackups(PlaylistBackupsRepo(self), PlaylistBackupFiles())
        self.audio_vibes = AudioVibes(AudioVibesRepo(self),
            sample_files.SampleFiles(sample_files.root_for_database(self._db_path, self._evidence_key)),
            self.prepared_content, AudioMetadataFiles(
                metadata_runtime_files.root_for_database(self._db_path, self._evidence_key),
                metadata_delivery_files.root_for_database(self._db_path, self._evidence_key)))
        self.search = Search(SearchRepo(self))
        from yt_playlist.controllers.enrichment import Enrichment
        self.enrichment = Enrichment(EnrichmentRepo(self), self.metadata_choices,
            RuntimeMetadataFiles(metadata_runtime_root(self._db_path, self._evidence_key)), self.artists)
        self.enrichment_reports = EnrichmentReports(EnrichmentReportsRepo(self),
            partial(metadata_runtime_files.read_at,
                    metadata_runtime_files.root_for_database(self._db_path, self._evidence_key)))
        from yt_playlist.controllers.enrichment_state import EnrichmentState
        from yt_playlist.providers.enrichment import load_config
        self.enrichment_state = EnrichmentState(EnrichmentStateRepo(self), self.metadata_choices,
            RuntimeMetadataFiles(metadata_runtime_root(self._db_path, self._evidence_key)),
            MetadataDeliveryFiles(metadata_delivery_root(self._db_path, self._evidence_key)), self.metadata_deliveries,
            self.enrichment, lambda: [provider['name'] for provider in load_config(self) if provider['enabled']])
        self.conn._close_callbacks.append(self.enrichment_state.files.close)
        from yt_playlist.core.maintenance_context import MaintenanceDatabase
        maintenance = MaintenanceDatabase(self.conn, self._lock, self._db_path, lambda: self.activity_paused)
        from yt_playlist.library.retention_composition import managed_retention
        from yt_playlist.controllers.collection_retention import CollectionRetention
        from yt_playlist.repos.collection_retention import CollectionRetentionRepo
        self.retention = Retention(maintenance, self.enrichment_state, self.vibe_models, self.embeddings,
            self.musical_profiles, self.vibe_predictions,
            managed_retention(maintenance, getattr(self, '_evidence_source', self)._playback_feedback, self._evidence_key),
            CollectionRetention(maintenance, CollectionRetentionRepo(maintenance),
                collection_recovery.root_for_database(self._db_path, self._evidence_key)))
        self.enrichment_queue = EnrichmentQueue(EnrichmentQueueRepo(self), self.enrichment_state,
            read_snapshot=self.read_snapshot, memoized_read=self.memoized_read,
            read_only=getattr(self, '_read_only', False))
        self.wiki = WikipediaCards(WikiRepo(self))
        self.modes = ModesRepo(self)
        self.recommendations = RecommendationPublications(RecommendationPublicationsRepo(self), self.rec_query,
            self.discovery, self.modes, RecommendationFiles(
                recommendation_outputs.root_for_database(self._db_path, self._evidence_key)))
        from yt_playlist.controllers.compact_taste_groups import CompactTasteGroups
        from yt_playlist.repos.group_retention import GroupRetentionRepo
        self.group_retention = GroupRetentionRepo(self)
        self.taste_groups = CompactTasteGroups(CompactTasteGroupsRepo(self), group_documents.GroupFiles(
            group_documents.root_for_database(self._db_path, self._evidence_key),
            policy_documents.root_for_database(self._db_path, self._evidence_key)),
            profile_files.ProfileFiles(file_paths.root_for_database(self._db_path, self._evidence_key, 'profiles')),
            self.group_retention, policies)
        self.trends = Trends(TrendsRepo(self), self.rec_proposals)
        from yt_playlist.controllers.history import History
        from yt_playlist.controllers.history_maintenance import HistoryMaintenance
        from yt_playlist.core.history_files import HistoryRepairFiles
        history = HistoryRepo(self)
        self.history = History(history, self.artists, HistoryMaintenance(
            history, self.settings, self.discovery, self.embeddings, self.rec_model, self.model_selection,
            self.rec_proposals, HistoryRepairFiles(), lambda: self._lock, self.listening_calendar, self.rec_refresh),
            lambda: self.transient_radio_playlists, lambda: self._lock, self.listening_calendar)

