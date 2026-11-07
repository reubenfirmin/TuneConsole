"""Action decisions and file admission run over mocked SQL ownership."""
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.actions import Actions
from yt_playlist.core.action_backup_files import ActionBackupFiles
from yt_playlist.core.collection_recovery import RecoveryFiles
from yt_playlist.repos.actions import ActionRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def actions():
    repo = create_autospec(ActionRepo, instance=True, spec_set=True)
    files = create_autospec(RecoveryFiles, instance=True, spec_set=True)
    backups = create_autospec(ActionBackupFiles, instance=True, spec_set=True)
    repo.transaction.side_effect = files.retained.side_effect = nullcontext
    repo.has_durable_targets.return_value = True
    repo.next_action_id.return_value = 7
    repo.prior_intent.return_value = None
    repo.backups.return_value = []
    repo.step_input.return_value = (10, None)
    repo.header.return_value = {'id': 7, 'idempotency_key': 'action:7'}
    files.publish.return_value = ('retained', 'digest', 100)
    return Actions(repo, files, backups, '/library', nullcontext)


def test_failed_recovery_publication_cannot_commit_an_action_reference(actions):
    actions.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError):
        actions.record_action('merge', '{}', '{}', 'planned', '{}', 1)
    actions.repository.insert_action.assert_not_called()


def test_idempotency_conflict_is_checked_before_file_publication(actions):
    actions.repository.prior_intent.return_value = {'id': 7, 'intent_digest': 'another-intent'}
    with pytest.raises(ValueError, match='another action intent'):
        actions.record_action('merge', '{}', '{}', 'planned', '{}', 1, idempotency_key='key')
    actions.files.publish.assert_not_called()
    actions.repository.insert_action.assert_not_called()


def test_matching_idempotent_intent_reuses_the_committed_action(actions):
    actions.record_action('merge', '{}', '{}', 'planned', '{}', 1, idempotency_key='key')
    digest = actions.repository.insert_action.call_args.kwargs['intent_digest']
    actions.repository.prior_intent.return_value = {'id': 7, 'intent_digest': digest}
    actions.files.publish.reset_mock()
    actions.repository.insert_action.reset_mock()
    assert actions.record_action('merge', '{}', '{}', 'planned', '{}', 2, idempotency_key='key') == 7
    actions.files.publish.assert_not_called()
    actions.repository.insert_action.assert_not_called()


def owned_action(status='executed'):
    return dict(layout='owned', row=dict(id=7, kind='delete_playlist', status=status, created_at=1, executed_at=2,
        revision='current', execution_token=None, recovery_filename='file', recovery_digest='digest', recovery_bytes=100),
        targets=[], steps=[], backups=[], resolutions=[], items=[], handles=[], draft=None, placement=None)


def test_stale_revision_cannot_publish_replacement_recovery(actions):
    actions.repository.get_action.return_value = owned_action()
    with pytest.raises(ValueError, match='updated by another worker'):
        actions.update_action(7, 'executed', 3, expected_revision='old')
    actions.files.read.assert_not_called()
    actions.files.publish.assert_not_called()
    actions.repository.update_action.assert_not_called()


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
    (None, None, False, True), ({'lease_until': 101}, None, False, False),
    ({'lease_until': 100}, None, False, True), (None, 7, False, False),
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
        actions.repository.claim_target.assert_called_once_with(11, 3, 4, token, 160)
        assert actions.repository.mark_step.called is (not reconcile)
    else:
        with pytest.raises(ValueError):
            actions.begin_deletion(7, 3, 'remote', 100, reconcile=reconcile)
        actions.repository.claim_target.assert_not_called()
        actions.repository.mark_step.assert_not_called()
