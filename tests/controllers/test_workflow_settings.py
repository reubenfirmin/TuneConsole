"""Receipt orchestration uses SQL and file mocks, without a test database."""
from contextlib import nullcontext
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.settings import Settings
from yt_playlist.controllers.workflow_settings import WorkflowSettings
from yt_playlist.core.collection_recovery import RecoveryFiles
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.core import workflow_settings as workflows
from yt_playlist.repos.settings import SettingsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def service():
    repository = create_autospec(SettingsRepo, instance=True, spec_set=True)
    repository.workflow_write_facts.return_value = {'prior': None, 'known': 7, 'next_id': 12}
    repository.reserve_file.side_effect = [41, 42]
    files = create_autospec(RecoveryFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.describe.return_value = ('digest', 50)
    files.publish_reserved.return_value = ('receipt', 'digest', 50)
    return WorkflowSettings(repository, files)


def test_transfer_writes_evidence_before_sql_and_strips_the_worker_token(service):
    events = []
    service.repository.reserve_file.side_effect = lambda *args: events.append('reserve') or 41
    service.files.publish_reserved.side_effect = lambda *args, **kwargs: events.append('file') or ('receipt', 'digest', 50)
    service.repository.save_workflows.side_effect = lambda *args: events.append('sql') or 'next'
    value = {'creation_phase': 'prepared', 'marker': 'marker', workflows.TOKEN: ''}
    assert service.save_transfer(7, 'fingerprint', value) == 'next'
    assert events == ['reserve', 'file', 'sql']
    service.files.publish_reserved.assert_called_once_with(41, 'spotify-transfer', 12,
        {'creation_phase': 'prepared', 'marker': 'marker'}, expected=('digest', 50))
    kind, prepared, reference = service.repository.save_workflows.call_args.args[0][0]
    assert kind == 'receipt'
    assert prepared['expected_revision'] == ''
    assert reference == ('receipt', 'digest', 50)


@pytest.mark.parametrize('value', [
    {'creation_phase': 'created', 'marker': 'changed', 'ytm_playlist_id': 'remote'},
    {'creation_phase': 'created', 'marker': 'marker', 'ytm_playlist_id': 'changed'},
    {'creation_phase': 'created', 'marker': 'marker', 'ytm_playlist_id': 'remote', 'source_video_ids': [3]}])
def test_transfer_rejects_changed_targets_and_invalid_slots_before_files(service, value):
    service.repository.workflow_write_facts.return_value = {'prior': {
        'id': 12, 'revision': 'current', 'marker': 'marker', 'provider_playlist_id_snapshot': 'remote'},
        'known': 7, 'next_id': 13}
    with pytest.raises(ValueError):
        service.save_transfer(7, 'fingerprint', {**value, workflows.TOKEN: 'current'})
    service.files.publish_reserved.assert_not_called()
    service.repository.save_workflows.assert_not_called()


def test_stale_transfer_never_writes_evidence(service):
    service.repository.workflow_write_facts.return_value = {'prior': {
        'id': 12, 'revision': 'current', 'marker': None, 'provider_playlist_id_snapshot': None},
        'known': 7, 'next_id': 13}
    with pytest.raises(ValueError, match='stale revision'):
        service.save_transfer(7, 'fingerprint', {workflows.TOKEN: 'old'})
    service.files.publish_reserved.assert_not_called()


def test_file_failure_does_not_persist_the_transfer(service):
    service.files.publish_reserved.side_effect = OSError('disk full')
    with pytest.raises(OSError):
        service.save_transfer(7, 'fingerprint', {})
    service.repository.save_workflows.assert_not_called()


def test_family_batch_assigns_distinct_sql_owner_ids_and_keeps_null_slots(service):
    key = workflows.PLAYLIST+':7'
    plans = service.prepare_values({key: json.dumps({'first': {'source_video_ids': ['one', None]}, 'second': {}})})
    assert [prepared['identity'] for _, prepared, _ in plans] == [12, 13]
    assert plans[0][1]['value']['source_video_ids'] == ['one', None]
    assert [call.args[0] for call in service.files.publish_reserved.call_args_list] == [41, 42]
    assert [call.args[2] for call in service.files.publish_reserved.call_args_list] == [12, 13]


def test_family_validates_every_receipt_before_publishing_any(service):
    with pytest.raises(ValueError, match='source slots'):
        service.prepare_values({workflows.PLAYLIST+':7': json.dumps({'valid': {}, 'invalid': {'source_video_ids': [False]}})})
    service.files.publish_reserved.assert_not_called()


def test_receipt_family_delete_does_not_write_files(service):
    assert service.prepare_values({workflows.PLAYLIST+':7': None}) == [('delete', ('playlist', 7), None)]
    service.files.publish_reserved.assert_not_called()


def test_receipt_reads_overlay_sql_fields_and_keep_opaque_evidence(service):
    service.repository.workflow_documents.return_value = [{
        'id': 12, 'fingerprint': 'fingerprint', 'revision': 'current', 'creation_phase': 'created',
        'marker': 'marker', 'provider_playlist_id_snapshot': 'remote', 'complete': 1, 'order_verified': 0,
        'updated_at': 20, 'last_run_added': 2, 'slots': ['one', None],
        'recovery_filename': 'receipt', 'recovery_digest': 'digest', 'recovery_bytes': 50}]
    service.files.read.return_value = {'creation_phase': 'prepared', 'complete': False, 'order_verified': True,
        'source_video_ids': [], 'evidence': {'matching': 'retained'}}
    assert service.receipts(7, token=True) == {'fingerprint': {
        'creation_phase': 'created', 'complete': True, 'order_verified': False, 'source_video_ids': ['one', None],
        'evidence': {'matching': 'retained'}, workflows.TOKEN: 'current'}}
    service.files.read.assert_called_once_with(('receipt', 'digest', 50), kind='spotify-transfer', owner=12)


def test_settings_prefix_reads_captured_manifests_outside_the_repository(service):
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    service.repository.prefix_documents.return_value = ({'ordinary': 'value'}, [workflows.PLAYLIST+':7'])
    service.repository.workflow_documents.return_value = []
    settings = Settings(service.repository, files, service.files)
    assert settings.get_settings_with_prefix('') == {'ordinary': 'value', workflows.PLAYLIST+':7': None}
    service.repository.prefix_documents.assert_called_once_with('')
