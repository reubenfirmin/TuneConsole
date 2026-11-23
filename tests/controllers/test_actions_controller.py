"""Action decisions and file admission run over mocked SQL ownership."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.actions import Actions
from yt_playlist.core.action_backup_files import ActionBackupFiles
from yt_playlist.core.collection_files import RecoveryFiles
from yt_playlist.repos.actions import ActionRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def actions():
    repo = create_autospec(ActionRepo, instance=True, spec_set=True)
    files = create_autospec(RecoveryFiles, instance=True, spec_set=True)
    backups = create_autospec(ActionBackupFiles, instance=True, spec_set=True)
    in_transaction = False

    @contextmanager
    def transaction():
        nonlocal in_transaction
        assert not in_transaction
        in_transaction = True
        try:
            yield
        finally:
            in_transaction = False

    def publish(*args, **kwargs):
        assert not in_transaction, 'File publication must precede the SQL transaction'
        return 'retained', 'digest', 100

    repo.transaction.side_effect = transaction
    files.retained.side_effect = nullcontext
    repo.next_action_id.return_value = 7
    repo.prior_intent.return_value = None
    repo.backups.return_value = []
    repo.step_input.return_value = (10, None)
    repo.header.return_value = {'id': 7, 'idempotency_key': 'action:7'}
    repo.reserve_file.return_value = 41
    repo.publish_record.side_effect = lambda prepared: prepared['identity']
    files.describe.return_value = ('digest', 100)
    files.publish_reserved.side_effect = publish
    return Actions(repo, files, backups, '/library', nullcontext)


def test_failed_recovery_publication_cannot_commit_an_action_reference(actions):
    actions.files.publish_reserved.side_effect = OSError('full')
    with pytest.raises(OSError):
        actions.record_action('merge', '{}', '{}', 'planned', '{}', 1)
    actions.repository.insert_action.assert_not_called()
    actions.repository.publish_record.assert_not_called()
    actions.repository.reserve_file.assert_called_once_with('action', 7, ('digest', 100), new_owner=True)


def test_idempotency_conflict_is_checked_before_file_publication(actions):
    actions.repository.prior_intent.return_value = {'id': 7, 'intent_digest': 'another-intent'}
    with pytest.raises(ValueError, match='another action intent'):
        actions.record_action('merge', '{}', '{}', 'planned', '{}', 1, idempotency_key='key')
    actions.files.publish_reserved.assert_not_called()
    actions.repository.reserve_file.assert_not_called()
    actions.repository.insert_action.assert_not_called()


def test_matching_idempotent_intent_reuses_the_committed_action(actions):
    actions.record_action('merge', '{}', '{}', 'planned', '{}', 1, idempotency_key='key')
    digest = actions.repository.publish_record.call_args.args[0]['intent_digest']
    actions.repository.prior_intent.return_value = {'id': 7, 'intent_digest': digest}
    actions.files.publish_reserved.reset_mock()
    actions.repository.publish_record.reset_mock()
    assert actions.record_action('merge', '{}', '{}', 'planned', '{}', 2, idempotency_key='key') == 7
    actions.files.publish_reserved.assert_not_called()
    actions.repository.publish_record.assert_not_called()


def owned_action(status='executed'):
    return dict(row=dict(id=7, kind='delete_playlist', status=status, created_at=1, executed_at=2,
        revision='current', execution_token=None, recovery_filename='file', recovery_digest='digest', recovery_bytes=100),
        targets=[], steps=[], backups=[])


def test_stale_revision_cannot_publish_replacement_recovery(actions):
    actions.repository.get_action.return_value = owned_action()
    with pytest.raises(ValueError, match='updated by another worker'):
        actions.update_action(7, 'executed', 3, expected_revision='old')
    actions.files.read.assert_not_called()
    actions.files.publish_reserved.assert_not_called()
    actions.repository.update_action.assert_not_called()


def test_record_commits_its_reservation_before_file_and_final_sql_publication(actions):
    events = []
    actions.repository.reserve_file.side_effect = lambda *args, **kwargs: events.append('reserve') or 41
    actions.files.publish_reserved.side_effect = lambda *args, **kwargs: events.append('file') or ('retained', 'digest', 100)
    actions.repository.publish_record.side_effect = lambda prepared: events.append('publish') or prepared['identity']
    assert actions.record_action('merge', '{}', '{}', 'planned', '{}', 1) == 7
    assert events == ['reserve', 'file', 'publish']
    actions.repository.transaction.assert_not_called()


@pytest.mark.parametrize('interrupted', [False, True])
def test_backup_is_copied_before_the_action_commits(actions, interrupted):
    events = []
    actions.backups.reference.return_value = (('original', 'digest', 100, 'verified'), {})

    def retain(*args, **kwargs):
        events.append('file')
        if interrupted:
            raise OSError('backup copy interrupted')
        return 'retained'

    actions.backups.retain.side_effect = retain
    if interrupted:
        with pytest.raises(OSError, match='backup copy interrupted'):
            actions.record_action('remove_track', '{}', '{}', 'executed', '{"backups":["original"]}', 1)
        actions.repository.publish_record.assert_not_called()
    else:
        actions.record_action('remove_track', '{}', '{}', 'executed', '{"backups":["original"]}', 1)
        progress = actions.repository.publish_record.call_args.args[0]['progress']
        assert progress['backups'] == [(0, ('original', 'digest', 100, 'verified'), 'retained')]
    assert events == ['file']
    actions.backups.retain.assert_called_once_with('original', expected=('digest', 100))
    actions.repository.transaction.assert_not_called()


def test_progress_file_failure_cannot_commit_an_action_update(actions):
    actions.repository.get_action.return_value = owned_action()
    actions.files.read.return_value = {'params': '{}', 'plan': '{}', 'undo': '{}'}
    actions.files.publish_reserved.side_effect = OSError('full')
    with pytest.raises(OSError, match='full'):
        actions.update_action(7, 'executed', 3, undo_json='{}')
    actions.repository.publish_update.assert_not_called()
    actions.repository.transaction.assert_not_called()


def test_undo_prepares_both_documents_before_its_single_sql_commit(actions):
    actions.repository.get_action.return_value = owned_action('undoing')
    actions.files.read.return_value = {'params': '{}', 'plan': '{}', 'undo': '{}'}
    actions.repository.next_action_id.return_value = 8
    events = []
    actions.files.publish_reserved.side_effect = lambda *args, **kwargs: events.append(('file', args[2])) or ('retained', 'digest', 100)
    actions.repository.finish_undo.side_effect = lambda *args: events.append(('finish', None))
    actions.finish_undo(7, {}, 3, claim_token='claim')
    assert events == [('file', 7), ('file', 8), ('finish', None)]
    prepared, history, token = actions.repository.finish_undo.call_args.args
    assert prepared['status'] == 'undone'
    assert history['undo_of'] == 7
    assert token == 'claim'
    actions.repository.transaction.assert_not_called()


def test_failed_undo_history_file_cannot_commit_undo_completion(actions):
    actions.repository.get_action.return_value = owned_action('undoing')
    actions.files.read.return_value = {'params': '{}', 'plan': '{}', 'undo': '{}'}
    actions.repository.next_action_id.return_value = 8
    actions.files.publish_reserved.side_effect = [('retained', 'digest', 100), OSError('full')]
    with pytest.raises(OSError, match='full'):
        actions.finish_undo(7, {}, 3, claim_token='claim')
    actions.repository.finish_undo.assert_not_called()
    actions.repository.clear_execution.assert_not_called()


@pytest.mark.parametrize('status', ['pending', 'draft', 'undone', 'cancelled'])
def test_non_undoable_states_cannot_acquire_an_execution_claim(actions, status):
    actions.repository.get_action.return_value = owned_action(status)
    actions.files.read.side_effect = lambda *args, **kwargs: deepcopy({'params': '{}', 'plan': '{}', 'undo': '{}'})
    with pytest.raises(ValueError, match='not undoable'):
        actions.claim_undo(7, 3)
    actions.repository.claim_execution.assert_not_called()


def test_deletion_verifies_committed_backup_before_claiming_or_marking_uncertain(actions):
    actions.repository.deletion_step.return_value = dict(id=10, target_id=11, phase='pending', outcome='not_sent',
        backup_status='verified', backup_filename='backup', backup_digest='digest', backup_bytes=100)
    actions.backups.reference.return_value = (('backup', 'changed', 100, 'verified'), {})
    with pytest.raises(ValueError, match='backup changed'):
        actions.begin_deletion(7, 3, 'remote', 100)
    actions.repository.claim_target.assert_not_called()
    actions.repository.mark_step.assert_not_called()


@pytest.mark.parametrize('prior,uncertain,reconcile,allowed', [
    (None, None, False, True), ({'id': 11, 'claim_until': 101}, None, False, False),
    ({'id': 12, 'claim_until': 100}, None, False, True), (None, 7, False, False),
    (None, 7, True, True), (None, 8, True, False),
])
def test_target_claims_expire_without_authorizing_uncertain_remote_replay(actions, prior, uncertain, reconcile, allowed):
    actions.repository.deletion_step.return_value = dict(id=10, target_id=11, phase='pending', outcome='not_sent',
        backup_status='verified', backup_filename='backup', backup_digest='digest', backup_bytes=100)
    actions.backups.reference.return_value = (('backup', 'digest', 100, 'verified'), {})
    actions.repository.claim_facts.return_value = ({'action_id': 7, 'account_id': 3, 'playlist_scope_id': 4}, prior, uncertain)
    if allowed:
        target, token = actions.begin_deletion(7, 3, 'remote', 100, reconcile=reconcile)
        assert target == 11
        actions.repository.claim_target.assert_called_once_with(11, token, 160, previous=prior['id'] if prior else None)
        assert actions.repository.mark_step.called is (not reconcile)
    else:
        with pytest.raises(ValueError):
            actions.begin_deletion(7, 3, 'remote', 100, reconcile=reconcile)
        actions.repository.claim_target.assert_not_called()
        actions.repository.mark_step.assert_not_called()
