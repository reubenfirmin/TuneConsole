"""History maintenance orders independent owners without opening SQLite."""
from contextlib import nullcontext
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.embeddings import Embeddings
from yt_playlist.controllers.history_maintenance import HistoryMaintenance
from yt_playlist.controllers.listening_calendar import ListeningCalendar
from yt_playlist.controllers.model_selection import ModelSelection
from yt_playlist.core.history_files import HistoryRepairFiles
from yt_playlist.core.repair import PHANTOM_PURGE_SEEN
from yt_playlist.controllers.discovery import Discovery
from yt_playlist.repos.history import HistoryRepo
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.settings import Settings

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def maintenance():
    def mock(cls):
        return create_autospec(cls, instance=True, spec_set=True)
    service = HistoryMaintenance(mock(HistoryRepo), mock(Settings), mock(Discovery),
        mock(Embeddings), mock(ModelSelection), mock(RecommendationProposals), mock(HistoryRepairFiles), nullcontext,
        mock(ListeningCalendar))
    service.repository.calendar_accounts.return_value = [3, 4]
    service.repository.verified_imports.return_value = [
        {'id': 1, 'account_id': 3, 'coverage_start': 10 * 86400 + 10, 'coverage_end': 20 * 86400 + 10},
        {'id': 2, 'account_id': 4, 'coverage_start': 40 * 86400, 'coverage_end': 50 * 86400}]
    service.repository.contradicted_observations.return_value = [{'id': 8, 'account_id': 3}]
    service.settings.get_setting.return_value = None
    return service


def test_verified_repair_excludes_partial_days_and_keeps_disjoint_account_windows(maintenance):
    maintenance.repair_verified_imports()
    maintenance.repository.contradicted_observations.assert_called_once_with([(3, 11, 19), (4, 40, 49)], 1)


def test_repair_publishes_recovery_before_deletion_and_acknowledges_last(maintenance):
    order = Mock()
    order.attach_mock(maintenance.files.publish, 'file')
    order.attach_mock(maintenance.repository.delete_observations, 'delete')
    order.attach_mock(maintenance.settings.set_setting, 'ack')
    maintenance.repair_verified_imports()
    assert [call[0] for call in order.mock_calls] == ['file', 'delete', 'ack']
    maintenance.repository.delete_observations.assert_called_once_with([8])
    watermark = maintenance.settings.set_setting.call_args.args[1]
    maintenance.settings.get_setting.return_value = watermark
    maintenance.repair_verified_imports()
    assert maintenance.files.publish.call_count == 1


@pytest.mark.parametrize('owner,method', [('files', 'publish'), ('repository', 'delete_observations')])
def test_failed_repair_leaves_watermark_pending(maintenance, owner, method):
    getattr(getattr(maintenance, owner), method).side_effect = OSError('injected failure')
    maintenance.repair_verified_imports()
    maintenance.settings.set_setting.assert_not_called()
    if owner == 'files':
        maintenance.repository.delete_observations.assert_not_called()


def test_no_verified_import_cannot_authorize_repair(maintenance):
    maintenance.repository.verified_imports.return_value = []
    maintenance.repair_verified_imports()
    maintenance.repository.contradicted_observations.assert_not_called()
    maintenance.files.publish.assert_not_called()


def test_empty_repair_acknowledges_without_allocating_recovery(maintenance):
    maintenance.repository.contradicted_observations.return_value = []
    maintenance.repair_verified_imports()
    maintenance.files.publish.assert_not_called()
    maintenance.repository.delete_observations.assert_not_called()
    assert maintenance.settings.set_setting.call_args.args[0] == PHANTOM_PURGE_SEEN


def test_older_newly_verified_interval_is_part_of_repair_identity(maintenance):
    maintenance.repair_verified_imports()
    watermark = maintenance.settings.set_setting.call_args.args[1]
    maintenance.settings.get_setting.return_value = watermark
    maintenance.repository.verified_imports.return_value[0]['coverage_end'] += 86400
    maintenance.repair_verified_imports()
    assert maintenance.files.publish.call_count == 2
    assert maintenance.settings.set_setting.call_args.args[1] != watermark


def test_removal_invalidates_derived_state_before_deleting_evidence(maintenance):
    maintenance.repository.spotify_history_stats.return_value = {'events': 5}
    maintenance.discovery.remove_spotify_origins.return_value = 2
    order = Mock()
    for name, method in [('outputs', maintenance.proposals.discard_learned_outputs),
                         ('selection', maintenance.selection.discard_history_models),
                         ('vectors', maintenance.vectors.discard_history_models),
                         ('settings', maintenance.settings.delete_setting),
                         ('discovery', maintenance.discovery.remove_spotify_origins),
                         ('receipts', maintenance.repository.delete_spotify_receipts),
                         ('source', maintenance.repository.remove_listening_source),
                         ('calendar', maintenance.calendar.reconcile)]:
        order.attach_mock(method, name)
    assert maintenance.remove_spotify_history() == {'events': 5, 'artists_removed': 2}
    calls = [call[0] for call in order.mock_calls]
    assert calls[:3] == ['outputs', 'selection', 'vectors']
    assert calls.index('discovery') < calls.index('receipts') < calls.index('source')
    assert [call.args[0] for call in maintenance.repository.remove_listening_source.call_args_list] == ['spotify_export', 'spotify']
    assert [call.args[0] for call in maintenance.calendar.reconcile.call_args_list] == [3, 4]
    assert max(i for i, name in enumerate(calls) if name == 'source') < calls.index('calendar')
    assert [call.args[0] for call in maintenance.settings.delete_setting.call_args_list][-2:] == [
        'spotify_imported_at', 'spotify_import_report']


def test_failed_invalidation_keeps_source_evidence_for_retry(maintenance):
    maintenance.vectors.discard_history_models.side_effect = OSError('injected failure')
    with pytest.raises(OSError):
        maintenance.remove_spotify_history()
    maintenance.proposals.discard_learned_outputs.assert_called_once()
    maintenance.selection.discard_history_models.assert_called_once()
    maintenance.discovery.remove_spotify_origins.assert_not_called()
    maintenance.repository.delete_spotify_receipts.assert_not_called()
