from contextlib import contextmanager
import json
import sqlite3
from unittest.mock import Mock, create_autospec
import uuid

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.album_feedback import AlbumFeedback
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.repos.album_feedback import AlbumFeedbackRepo
from yt_playlist.repos.collection import CollectionRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def feedback():
    repository = create_autospec(AlbumFeedbackRepo, instance=True, spec_set=True)
    collection = create_autospec(CollectionRepo, instance=True, spec_set=True)
    writing = False

    @contextmanager
    def transaction():
        nonlocal writing
        writing = True
        try:
            yield
        finally:
            writing = False

    @contextmanager
    def fence(**kwargs):
        assert not writing
        yield

    def preferred(sources):
        assert not writing, 'Alias lookup must not join another repository transaction'
        return {source: 'release' if source in ('old', 'new') else source for source in sources}

    def read_original(plan):
        assert not writing, 'Prepared feedback files must be read before the write'
        return json.dumps({'old': {'kind': 'more', 'at': 1}})

    repository.transaction.side_effect = transaction
    repository.uses_rows.return_value = True
    repository.account.return_value = (1, True)
    repository.replacement_facts.return_value = (3, [])
    repository.save_legacy.return_value = True
    collection.preferred_album_ids.side_effect = preferred
    return AlbumFeedback(repository, collection, Mock(side_effect=fence), Mock(side_effect=read_original))


def previous(**values):
    return {'created_at': 10, 'withdrawn_at': None, 'opportunity_id': 'offer', 'action_id': 'original', **values}


def test_replacement_withdraws_each_original_action_once_and_supplies_new_response(feedback):
    feedback.repository.replacement_facts.return_value = (3, [previous(), previous(),
        previous(action_id='other', withdrawn_at=11), previous(action_id=None)])
    feedback.replace('new', 'known', 20, 'new-offer', account_id=2)
    arguments = feedback.repository.save_response.call_args.args
    assert arguments[:6] == (3, 1, 'new', 'known', 20, 'new-offer')
    action, intents = arguments[6:]
    assert action
    assert intents == [
        {'action_id': uuid.uuid5(uuid.NAMESPACE_URL, 'album-feedback-withdrawal:original').hex,
         'opportunity_id': 'offer', 'kind': 'withdraw', 'now': 20, 'target_action_id': 'original'},
        {'action_id': uuid.uuid5(uuid.NAMESPACE_URL, 'album-feedback-withdrawal:other').hex,
         'opportunity_id': 'offer', 'kind': 'withdraw', 'now': 20, 'target_action_id': 'other'},
        {'action_id': action, 'opportunity_id': 'new-offer', 'kind': 'feedback', 'now': 20,
         'option_id': 'new', 'feedback_kind': 'known'}]
    feedback.repository.account.assert_called_once_with(2)
    feedback.collection.preferred_album_ids.assert_not_called()


def test_clear_withdraws_previous_action_without_issuing_new_feedback(feedback):
    feedback.repository.replacement_facts.return_value = (3, [previous()])
    feedback.replace('new', 'clear', 20)
    arguments = feedback.repository.save_response.call_args.args
    assert arguments[6] is None
    assert [intent['kind'] for intent in arguments[7]] == ['withdraw']


@pytest.mark.parametrize('account,active,message', [
    (None, False, 'Choose an active account'), (2, False, 'requires an active account'),
])
def test_account_admission_precedes_any_feedback_write(feedback, account, active, message):
    feedback.repository.account.return_value = account, active
    with pytest.raises(ValueError, match=message):
        feedback.replace('new', 'more', 20)
    feedback.repository.save_response.assert_not_called()


def test_a_response_cannot_precede_the_current_judgment(feedback):
    feedback.repository.replacement_facts.return_value = (3, [previous(created_at=21)])
    with pytest.raises(ValueError, match='precedes the retained response'):
        feedback.replace('new', 'more', 20)
    feedback.repository.save_response.assert_not_called()


@pytest.mark.parametrize('now', [None, True, float('inf'), float('nan'), '20'])
def test_native_observation_time_is_validated_before_transaction(feedback, now):
    with pytest.raises(ValueError, match='finite observation time'):
        feedback.replace('new', 'more', now)
    feedback.repository.transaction.assert_not_called()


def test_native_read_preserves_original_ids_and_ignores_recovery_files(feedback):
    feedback.repository.original_facts.return_value = {'rows': [
        {'source_browse_id': 'old', 'response': 'known', 'created_at': 10,
         'opportunity_id': 'original-offer', 'action_id': 'original-action'}]}
    assert feedback.original() == {'old': {'kind': 'known', 'at': 10,
        'opportunity_id': 'original-offer', 'action_id': 'original-action'}}
    feedback.read_original.assert_not_called()


def test_legacy_replacement_combines_aliases_outside_the_sql_write(feedback):
    feedback.repository.uses_rows.return_value = False
    old = {'old': {'kind': 'more', 'at': 10, 'opportunity_id': 'offer', 'action_id': 'original'},
           'unrelated': {'kind': 'known', 'at': 11}}
    raw = json.dumps(old)
    feedback.repository.original_facts.return_value = {'raw': raw, 'selected': False, 'plan': None}
    feedback.replace('new', 'reject', 20, 'new-offer')
    before, updated, intents = feedback.repository.save_legacy.call_args.args
    assert before == raw
    result = json.loads(updated)
    assert set(result) == {'new', 'unrelated'}
    assert result['new']['kind'] == 'reject' and result['unrelated'] == old['unrelated']
    assert [intent['kind'] for intent in intents] == ['withdraw', 'feedback']
    feedback.repository.transaction.assert_not_called()


def test_prepared_legacy_document_is_read_before_sql_replacement(feedback):
    feedback.repository.uses_rows.return_value = False
    feedback.repository.original_facts.return_value = {'raw': 'original source bytes', 'selected': True, 'plan': 'plan'}
    feedback.replace('new', 'clear', 20)
    feedback.read_original.assert_called_once_with('plan')
    assert feedback.repository.save_legacy.call_args.args == ('original source bytes', '{}', [])


def test_legacy_compare_and_save_reloads_after_a_changed_original(feedback):
    feedback.repository.uses_rows.return_value = False
    feedback.repository.original_facts.side_effect = [
        {'raw': '{}', 'selected': False, 'plan': None},
        {'raw': '{"unrelated":{"kind":"known","at":11}}', 'selected': False, 'plan': None}]
    feedback.repository.save_legacy.side_effect = [False, True]
    feedback.replace('new', 'clear', 20)
    assert feedback.repository.save_legacy.call_count == 2
    assert json.loads(feedback.repository.save_legacy.call_args.args[1]) == {
        'unrelated': {'kind': 'known', 'at': 11}}


def test_legacy_compare_and_save_has_a_bounded_retry(feedback):
    feedback.repository.uses_rows.return_value = False
    feedback.repository.original_facts.return_value = {'raw': None, 'selected': False, 'plan': None}
    feedback.repository.save_legacy.return_value = False
    with pytest.raises(WorkBudgetExceeded, match='changed during replacement'):
        feedback.replace('new', 'clear', 20)
    assert feedback.repository.save_legacy.call_count == 3


def test_protection_combines_durable_and_in_memory_pins_under_the_fence(feedback):
    capture = Mock()

    @contextmanager
    def protection():
        yield ('artifact',), ('pending', 'shared')

    capture.protection.side_effect = protection
    feedback.repository.retained_opportunities.return_value = {'durable', 'shared'}
    with feedback.protection(capture) as pins:
        assert pins == (('artifact',), {'pending', 'durable', 'shared'})
    feedback.fence.assert_called_once_with(wait_seconds=0)


def test_retirement_rejects_uncommitted_writer_but_propagates_other_sql_errors(feedback):
    error = sqlite3.OperationalError('writer busy')
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    feedback.repository.retained_opportunities.side_effect = error
    with pytest.raises(WorkBudgetExceeded, match='has not committed'), feedback.protection():
        pytest.fail('Busy retirement must not be admitted')
    feedback.repository.retained_opportunities.side_effect = sqlite3.OperationalError('invalid query')
    with pytest.raises(sqlite3.OperationalError, match='invalid query'), feedback.protection():
        pytest.fail('Invalid query must not be hidden')


def test_empty_tail_wraps_to_the_beginning_of_the_outbox(feedback):
    feedback.repository.pending_responses.side_effect = [[], [{'id': 2}]]
    assert feedback.pending_responses(after=8, limit=4) == [{'id': 2}]
    assert [call.kwargs for call in feedback.repository.pending_responses.call_args_list] == [
        {'after': 8, 'limit': 4}, {'after': 0, 'limit': 4}]
