"""File-backed settings use mock SQL and file collaborators."""
from contextlib import nullcontext
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.settings import Settings
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.core.collection_recovery import RecoveryFiles
from yt_playlist.core import spotify_job_state, taste_control_state, experiment_decision_state
from yt_playlist.repos.settings import SettingsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def settings():
    repository = create_autospec(SettingsRepo, instance=True, spec_set=True)
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.describe.return_value = ('digest', 50)
    repository.reserve_output_file.return_value = 42
    recovery = create_autospec(RecoveryFiles, instance=True, spec_set=True)
    recovery.retained.side_effect = nullcontext
    repository.reserve_file.return_value = 41
    recovery.describe.return_value = ('digest', 50)
    return Settings(repository, files, recovery)


@pytest.mark.parametrize('method', ['set_values', 'publish_values'])
def test_saved_output_files_precede_the_settings_write(settings, method):
    events = []
    settings.files.publish_reserved.side_effect = lambda identity, encoded, **kwargs: events.append('file') or ('output', 'digest', len(encoded))
    writer = getattr(settings.repository, method)
    writer.side_effect = lambda *args, **kwargs: events.append('sql')
    values = {'rec_autotune_result': '{ "ran_at": 12 }', 'personal': 'value'}
    getattr(settings, method)(values)
    assert events == ['file', 'sql']
    prepared, durable, identities = writer.call_args.kwargs['outputs']
    assert writer.call_args.args == (values,)
    assert prepared['rec_autotune_result']['observed'] == 12
    assert durable['rec_autotune_result'][:2] == ('output', 'digest')
    assert identities == {'rec_autotune_result': 42}


def test_file_failure_keeps_settings_and_saved_output_unchanged(settings):
    settings.files.publish_reserved.side_effect = OSError('full disk')
    with pytest.raises(OSError):
        settings.set_values({'rec_autotune_result': '{}', 'personal': 'new'})
    settings.repository.set_values.assert_not_called()


@pytest.mark.parametrize('raw', ['[]', '{"menu_id": 3}', '{"comparison": [1]}',
                                 '{"comparison": {"run_id": true}}', '{"tracks": [{}]}'])
def test_invalid_seed_is_rejected_before_file_or_sql(settings, raw):
    with pytest.raises(ValueError):
        settings.set_setting('radio_seed_offer', raw)
    settings.files.publish_reserved.assert_not_called()
    settings.repository.set_values.assert_not_called()


def test_saved_output_delete_only_publishes_the_sql_deletion(settings):
    settings.delete_setting('radio_seed_offer')
    settings.repository.set_values.assert_called_once_with(
        {'radio_seed_offer': None}, outputs=({'radio_seed_offer': None}, {}, {}))
    settings.files.publish_reserved.assert_not_called()


def test_native_read_uses_detached_reference_and_missing_file_uses_default(settings):
    reference = {'filename': 'output', 'content_digest': 'digest', 'byte_size': 10}
    settings.repository.output_document.return_value = reference
    settings.files.read.return_value = 'original text'
    assert settings.get_setting('rec_autotune_result', 'fallback') == 'original text'
    settings.files.read.assert_called_once_with(reference, surface='rec_autotune_result', chunk_key='')
    settings.repository.get_setting.assert_not_called()
    settings.files.read.side_effect = FileNotFoundError('expired')
    assert settings.get_setting('rec_autotune_result', 'fallback') == 'fallback'


def test_empty_selection_does_not_read_the_old_setting(settings):
    settings.repository.output_document.return_value = None
    assert settings.get_setting('radio_seed_offer', 'fallback') == 'fallback'
    settings.repository.get_setting.assert_not_called()
    settings.files.read.assert_not_called()


@pytest.fixture
def import_jobs(settings):
    settings.repository.import_job_facts.return_value = {'head': None, 'known': 7, 'next_id': 12}
    settings.recovery.publish_reserved.return_value = ('report', 'digest', 50)
    return settings


def test_import_report_file_precedes_sql_admission(import_jobs):
    events = []
    import_jobs.repository.reserve_file.side_effect = lambda *args: events.append('reserve') or 41
    import_jobs.recovery.publish_reserved.side_effect = lambda *args, **kwargs: events.append('file') or ('report', 'digest', 50)
    import_jobs.repository.save_spotify_import_job.side_effect = lambda *args: events.append('sql') or 'new-revision'
    job = {'status': 'running', 'identity_id': 7, 'report': {'found': 2}, 'phase': 'import'}
    assert import_jobs.save_spotify_import_job(job) == 'new-revision'
    assert events == ['reserve', 'file', 'sql']
    import_jobs.recovery.publish_reserved.assert_called_once_with(41, 'spotify-import', 12, {'report': {'found': 2}},
                                                                expected=('digest', 50))
    prepared, reference = import_jobs.repository.save_spotify_import_job.call_args.args
    assert prepared['value']['account_id'] == 7
    assert prepared['identity'] == 12
    assert reference == ('report', 'digest', 50)


@pytest.mark.parametrize('job', [[], {'status': 'running', 'account_id': True},
    {'status': 'running', 'account_id': 7, '_job_revision': 'old'},
    {'status': 'running', 'account_id': 7, 'unknown': 2}])
def test_invalid_or_stale_import_job_never_publishes_a_file(import_jobs, job):
    with pytest.raises(ValueError):
        import_jobs.save_spotify_import_job(job)
    import_jobs.recovery.publish_reserved.assert_not_called()
    import_jobs.repository.save_spotify_import_job.assert_not_called()


def test_failed_import_report_file_keeps_the_current_job(import_jobs):
    import_jobs.recovery.publish_reserved.side_effect = OSError('disk full')
    with pytest.raises(OSError):
        import_jobs.save_spotify_import_job({'status': 'running', 'account_id': 7})
    import_jobs.repository.save_spotify_import_job.assert_not_called()


def test_import_job_read_combines_captured_fields_and_report(import_jobs):
    import_jobs.repository.import_job_document.return_value = {
        'id': 12, 'revision': 'current', 'status': 'complete', 'field_mask': 1 << spotify_job_state.FIELDS.index('dismissed'),
        'dismissed': 0, 'account_present': 1, 'account_id': None, 'unresolved_account_id': 7,
        'recovery_filename': 'report', 'recovery_digest': 'digest', 'recovery_bytes': 50}
    import_jobs.recovery.read.return_value = {'report': {'found': 2}}
    assert import_jobs.spotify_import_job() == {'status': 'complete', 'dismissed': False,
        'account_id': 7, '_job_revision': 'current', 'report': {'found': 2}}
    import_jobs.recovery.read.assert_called_once_with(('report', 'digest', 50), kind='spotify-import', owner=12)


def test_import_job_delete_clears_only_sql_state(import_jobs):
    import_jobs.delete_setting(spotify_job_state.KEY)
    import_jobs.repository.set_values.assert_called_once_with({spotify_job_state.KEY: None}, import_job=(None, None))
    import_jobs.recovery.publish_reserved.assert_not_called()


def test_import_job_batch_prepares_file_before_any_settings_write(import_jobs):
    import_jobs.set_values({spotify_job_state.KEY: '{"status":"complete","account_id":7}', 'ordinary': 'value'})
    prepared, reference = import_jobs.repository.set_values.call_args.kwargs['import_job']
    assert prepared['value']['status'] == 'complete'
    assert prepared['identity'] == 12
    assert reference == ('report', 'digest', 50)


def test_taste_checkpoint_file_precedes_its_parameter_and_weight_rows(settings):
    events = []
    settings.repository.reserve_file.side_effect = lambda *args: events.append('reserve') or 41
    settings.recovery.publish_reserved.side_effect = lambda *args, **kwargs: events.append('file') or ('checkpoint', 'digest', 50)
    settings.repository.set_values.side_effect = lambda *args, **kwargs: events.append('sql')
    raw = '{"saved_at":12,"params":{"rec_param:breadth_bias":".7"},"weights":[]}'
    settings.set_setting(taste_control_state.CHECKPOINT, raw)
    assert events == ['reserve', 'file', 'sql']
    settings.recovery.publish_reserved.assert_called_once_with(41, 'taste-checkpoint', 1, {'rec_param:breadth_bias': '.7'},
                                                              expected=('digest', 50))
    value, reference = settings.repository.set_values.call_args.kwargs['controls'][taste_control_state.CHECKPOINT]
    assert value['saved_at'] == 12
    assert reference == ('checkpoint', 'digest', 50)


@pytest.mark.parametrize('key,raw', [
    (taste_control_state.CHECKPOINT, '{"saved_at":1,"params":{"x":2},"weights":[]}'),
    (taste_control_state.BASELINE, '[{"key":"track"}]')])
def test_invalid_taste_control_never_writes_files_or_settings(settings, key, raw):
    with pytest.raises(ValueError):
        settings.set_setting(key, raw)
    settings.recovery.publish_reserved.assert_not_called()
    settings.repository.set_values.assert_not_called()


def test_taste_comparison_read_checks_trace_alignment_with_captured_entries(settings):
    settings.repository.taste_document.return_value = {
        'header': {'recovery_filename': 'comparison', 'recovery_digest': 'digest', 'recovery_bytes': 50},
        'entries': [{'current_key': 'redirected', 'rank': 1, 'score': .5, 'lane': 'content', 'lane_weight': 1}]}
    settings.recovery.read.return_value = [{'why': 'matched'}]
    assert json.loads(settings.get_setting(taste_control_state.BASELINE)) == [
        {'key': 'redirected', 'rank': 1, 'score': .5, 'lane': 'content', 'lane_weight': 1, 'trace': {'why': 'matched'}}]
    settings.recovery.read.assert_called_once_with(('comparison', 'digest', 50), kind='taste-comparison', owner=1)
    settings.recovery.read.return_value = []
    with pytest.raises(ValueError, match='ordered owner rows'):
        settings.get_setting(taste_control_state.BASELINE)


def test_taste_control_delete_keeps_expired_files_out_of_the_sql_write(settings):
    settings.delete_setting(taste_control_state.CHECKPOINT)
    settings.repository.set_values.assert_called_once_with({taste_control_state.CHECKPOINT: None},
        controls={taste_control_state.CHECKPOINT: (None, None)})
    settings.recovery.publish_reserved.assert_not_called()


def test_experiment_decision_and_stage_share_one_settings_publication(settings):
    item = {'decision': 'retire', 'cohort': 'all', 'reason': 'measured', 'at': 12,
            'powered': False, 'basis': 'observation', 'live_evidence': {'ppr': {}, 'cosine': {}}}
    values = {experiment_decision_state.KEY: json.dumps([item]), 'rec_experiment_stage': 'retired'}
    settings.publish_values(values)
    settings.repository.publish_values.assert_called_once_with(values)
    settings.recovery.publish_reserved.assert_not_called()
