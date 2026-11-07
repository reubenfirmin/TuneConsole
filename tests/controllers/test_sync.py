import json
from contextlib import contextmanager
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.sync import LibrarySync
from yt_playlist.core.collection_recovery import RecoveryFiles
from yt_playlist.controllers.settings import Settings
from yt_playlist.repos.sync import SyncRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def sync():
    repository = create_autospec(SyncRepo, instance=True, spec_set=True)
    repository.state_facts.return_value = {'raw': '{}'}
    repository.has_attempt_state.return_value = True
    settings = create_autospec(Settings, instance=True, spec_set=True)
    files = create_autospec(RecoveryFiles, instance=True, spec_set=True)
    files.publish.return_value = ('result.json', 'digest', 123)
    return LibrarySync(repository, settings, files, Mock())


def test_completion_publishes_prepared_progress_without_database_callbacks(sync):
    result = sync.finish_attempt(200, {'status': 'complete', 'accounts': {}, 'resources': {}},
                                 expected_revision=17)
    assert result == {'status': 'complete', 'accounts': {}, 'resources': {}, 'last_attempt_at': 200}
    sync.recovery.publish.assert_called_once_with('library-sync', 1, result)
    sync.repository.finish_attempt.assert_called_once_with(
        200, result, reference=('result.json', 'digest', 123), expected_revision=17, last_success=200)
    sync.settings.publish_values.assert_not_called()


def test_partial_attempt_keeps_prior_resource_success_and_does_not_advance_global_success(sync):
    sync.repository.state_facts.return_value = {'raw': json.dumps({'resources': {
        'album_tracks': {'status': 'complete', 'last_success_at': 100}}})}
    result = sync.finish_attempt(200, {'status': 'partial', 'resources': {
        'album_tracks': {'status': 'failed'}}})
    assert result['resources']['album_tracks'] == {
        'status': 'failed', 'last_attempt_at': 200, 'last_success_at': 100, 'failures': 1, 'retry_at': 500}
    assert sync.repository.finish_attempt.call_args.kwargs['last_success'] is None


def test_stale_attempt_fails_before_file_publication(sync):
    sync.repository.require_revision.side_effect = ValueError('superseded')
    with pytest.raises(ValueError, match='superseded'):
        sync.finish_attempt(200, {'status': 'complete'}, expected_revision=17)
    sync.recovery.publish.assert_not_called()
    sync.repository.finish_attempt.assert_not_called()
    sync.repository.ensure_attempt.assert_not_called()


def test_file_failure_never_installs_a_success_receipt(sync):
    sync.recovery.publish.side_effect = OSError('file interrupted')
    with pytest.raises(OSError, match='file interrupted'):
        sync.finish_attempt(200, {'status': 'complete', 'accounts': {}, 'resources': {}})
    sync.repository.finish_attempt.assert_not_called()
    sync.repository.ensure_attempt.assert_called_once_with(200)


def test_legacy_partial_attempt_preserves_global_success(sync):
    sync.repository.has_attempt_state.return_value = False
    result = sync.finish_attempt(200, {'status': 'partial', 'resources': {}})
    values = sync.settings.publish_values.call_args.args[0]
    assert json.loads(values['library_sync_state']) == result
    assert values['last_sync_outcome'] == 'partial'
    assert 'last_sync_at' not in values
    sync.repository.finish_attempt.assert_not_called()
    sync.recovery.publish.assert_not_called()


def test_file_protection_lives_until_recovery_has_been_read(sync):
    active = set()

    @contextmanager
    def lifetime(name, value):
        active.add(name)
        try:
            yield value
        finally:
            active.remove(name)

    sync.recovery.retained.side_effect = lambda: lifetime('files', None)
    attempt = {'recovery_filename': 'state.json', 'recovery_digest': 'digest', 'recovery_bytes': 123,
               'finished_at': 100, 'phase': 'running'}
    sync.repository.state_facts.return_value = {'attempt': attempt, 'cursors': []}

    def read(*args, **kwargs):
        assert active == {'files'}
        return {'status': 'complete'}

    sync.recovery.read.side_effect = read
    assert sync.state() == {'status': 'complete', 'last_attempt_at': 100}
    assert not active


def test_attempt_record_precedes_file_and_completion(sync):
    calls = []
    sync.repository.ensure_attempt.side_effect = lambda now: calls.append('record')
    sync.recovery.publish.side_effect = lambda *args: calls.append('file') or ('result.json', 'digest', 123)
    sync.repository.finish_attempt.side_effect = lambda *args, **kwargs: calls.append('complete')
    sync.finish_attempt(200, {'status': 'complete', 'accounts': {}, 'resources': {}})
    assert calls == ['record', 'file', 'complete']
