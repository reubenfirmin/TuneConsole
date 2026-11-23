"""Selection policy and restore ordering without a test database."""
from contextlib import nullcontext
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.embeddings import Embeddings
from yt_playlist.controllers.model_selection import ModelSelection
from yt_playlist.controllers.rec_model import RecommendationModel
from yt_playlist.repos.model_selection import ModelSelectionRepo
from yt_playlist.controllers.settings import Settings

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def selection():
    repo = create_autospec(ModelSelectionRepo, instance=True, spec_set=True)
    settings = create_autospec(Settings, instance=True, spec_set=True)
    vectors = create_autospec(Embeddings, instance=True, spec_set=True)
    vectors.retained.side_effect = nullcontext
    settings.get_setting.return_value = None
    model = create_autospec(RecommendationModel, instance=True, spec_set=True)
    return ModelSelection(repo, settings, vectors, model, nullcontext)


def test_selection_orders_files_restore_point_vectors_settings_and_result(selection):
    selection.vectors.prepare.return_value = 'prepared'
    selection.vectors.rows.return_value = [('original', b'old')]
    order = Mock()
    for name, method in [('files', selection.vectors.prepare), ('restore_point', selection.repository.save_previous),
                         ('vectors', selection.vectors.publish), ('settings', selection.model.publish_model_state),
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
    selection.model.publish_model_state.assert_not_called()
    selection.repository.write_result.assert_not_called()


def test_retention_keeps_recent_and_active_runs_and_freezes_expired_forecast_summary(selection):
    selection.repository.retention_facts.return_value = {'recent': [1],
        'expired': [{'id': 99, 'followup': {'trials': 8, 'selected_hits': 3}}]}
    selection.settings.get_setting.side_effect = {'rec_selection_active_run': '3',
                                                  'rec_selection_display_run': '4'}.get
    now = 200 * 86400
    selection.maintain_selection(now)
    keep, frozen, expired = selection.repository.prune.call_args.args
    assert keep == {'1', '3', '4'}
    assert expired == [99]
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
    selection.repository.retire_model_display.assert_not_called()
    selection.settings.publish_values.assert_not_called()
    selection.repository.write_result.assert_not_called()


def test_schedule_uses_daily_model_schedule(selection):
    selection.repository.schedule_state.return_value = {'next_at': 200}
    assert selection.next_daily_at() == 200
    selection.schedule_daily(300, 100)
    selection.repository.publish_schedule.assert_called_once_with(300, 100)
