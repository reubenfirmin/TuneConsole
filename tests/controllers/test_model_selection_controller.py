"""Selection policy and restore ordering without a test database."""
from contextlib import nullcontext
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.embeddings import Embeddings
from yt_playlist.controllers.model_selection import ModelSelection
from yt_playlist.repos.model_selection import ModelSelectionRepo
from yt_playlist.controllers.radio_checkpoints import RadioCheckpoints
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.settings import Settings

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def selection():
    repo = create_autospec(ModelSelectionRepo, instance=True, spec_set=True)
    settings = create_autospec(Settings, instance=True, spec_set=True)
    proposals = create_autospec(RecommendationProposals, instance=True, spec_set=True)
    radio = create_autospec(RadioCheckpoints, instance=True, spec_set=True)
    vectors = create_autospec(Embeddings, instance=True, spec_set=True)
    vectors.retained.side_effect = nullcontext
    settings.get_setting.return_value = None
    return ModelSelection(repo, settings, proposals, radio, vectors, nullcontext)


def test_selection_orders_files_restore_point_vectors_settings_and_result(selection):
    selection.vectors.prepare.return_value = 'prepared'
    selection.vectors.rows.return_value = [('original', b'old')]
    order = Mock()
    for name, method in [('files', selection.vectors.prepare), ('restore_point', selection.repository.save_previous),
                         ('vectors', selection.vectors.publish), ('settings', selection.settings.publish_values),
                         ('result', selection.repository.write_result)]:
        order.attach_mock(method, name)
    selection.publish_selection(8, '{}', [('new', b'new')], {'method': 'svd', 'dim': 3})
    assert [call[0] for call in order.mock_calls] == ['files', 'restore_point', 'vectors', 'settings', 'result']
    selection.repository.save_previous.assert_called_once_with(8, [('original', b'old')])


def test_paused_selection_never_writes_files_or_sql(selection):
    selection.settings.get_setting.return_value = '1'
    with pytest.raises(ValueError, match='paused'):
        selection.publish_selection(8, '{}', [], {'method': 'svd', 'dim': 3})
    selection.vectors.prepare.assert_not_called()
    assert not selection.repository.mock_calls


def test_failed_vector_publication_never_changes_configuration_or_status(selection):
    selection.vectors.publish.side_effect = ValueError('rejected vector')
    with pytest.raises(ValueError):
        selection.publish_selection(8, '{}', [], {'method': 'svd', 'dim': 3})
    selection.repository.save_previous.assert_called_once()
    selection.settings.publish_values.assert_not_called()
    selection.repository.write_result.assert_not_called()


def test_retention_keeps_all_held_runs_and_freezes_expired_forecast_summary(selection):
    selection.repository.retention_facts.return_value = {'recent': [1], 'radio_seeds': [2],
        'pairs': ['model_pair:1:incumbent:mixes', 'model_pair:99:challenger:radio'],
        'expired': [{'id': 99, 'followup': {'trials': 8, 'selected_hits': 3}}], 'expired_ids': [99]}
    selection.settings.get_setting.side_effect = lambda key: '3' if key == 'rec_selection_active_run' else None
    selection.radio.load.return_value = ({'comparison': {'run_id': 4}}, None)
    selection.proposals.get_proposals.side_effect = lambda surface: (
        {'comparison': {'run_id': 5}} if surface == 'home_choice' else {'choice_context': {'comparison': {'run_id': 6}}})
    now = 200 * 86400
    selection.maintain_selection(now)
    keep, removed, frozen, expired = selection.repository.prune.call_args.args
    assert keep == {'1', '2', '3', '4', '5', '6'}
    assert removed == ['model_pair:99:challenger:radio'] and expired == [99]
    assert json.loads(frozen[0][0])['followup'] == {
        'trials': 8, 'selected_hits': 3, 'frozen_at': now, 'reprocessing_available': False, 'retention_days': 180}


def test_frozen_forecast_series_cannot_restart(selection):
    selection.repository.selection_run.return_value = {'id': 8, 'followup': {'frozen_at': 100}}
    selection.save_model_forecast(8, 200, {})
    selection.repository.insert_forecast.assert_not_called()


def test_restore_rejects_obsolete_catalog_identity_before_file_io(selection):
    selection.repository.restore_facts.return_value = {'identity_version': 0, 'result': {}, 'vectors': []}
    with pytest.raises(ValueError, match='identity change'):
        selection.restore_model_selection(8, 200)
    selection.vectors.prepare.assert_not_called()


def test_selected_partial_publication_keeps_its_original_restore_available(selection):
    selection.repository.restore_facts.return_value = {'identity_version': 2,
        'result': {'status': 'failed', 'previous': {'method': 'svd', 'dim': 2}}, 'vectors': [('old', b'old')]}
    selection.settings.get_setting.return_value = '8'
    selection.repository.selection_runs.return_value = []
    assert selection.restore_model_selection(8, 200)
    selection.vectors.prepare.assert_called_once_with('rec_vectors', [('old', b'old')], replace=True,
                                                     model_version='selection-restore:8')
    restored = json.loads(selection.repository.write_result.call_args.args[1])
    assert restored['status'] == 'restored'


def test_restore_file_failure_preserves_settings_outputs_and_run_status(selection):
    selection.repository.restore_facts.return_value = {'identity_version': 2,
        'result': {'status': 'applied', 'previous': {'method': 'svd', 'dim': 2}}, 'vectors': [('old', b'old')]}
    selection.vectors.prepare.side_effect = OSError('missing storage')
    with pytest.raises(OSError):
        selection.restore_model_selection(8, 200)
    selection.repository.clear_restored_outputs.assert_not_called()
    selection.settings.publish_values.assert_not_called()
    selection.repository.write_result.assert_not_called()


def test_schedule_uses_legacy_proposal_owner_when_typed_owner_is_absent(selection):
    selection.repository.schedule_state.return_value = (False, None)
    selection.repository.publish_schedule.return_value = False
    selection.proposals.get_proposals.return_value = {'next_at': 200}
    assert selection.next_daily_at() == 200
    selection.schedule_daily(300, 100)
    selection.proposals.put_proposals.assert_called_once_with('daily_model_schedule', {'next_at': 300}, 100)
