from contextlib import contextmanager
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.album_descriptions import AlbumDescriptions
from yt_playlist.repos.album_descriptions import AlbumDescriptionsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def descriptions():
    repository = create_autospec(AlbumDescriptionsRepo, instance=True, spec_set=True)
    writing = False

    @contextmanager
    def transaction():
        nonlocal writing
        assert not writing
        writing = True
        try:
            yield
        finally:
            writing = False

    def outside_transaction(value):
        def operation(*args):
            assert not writing, 'Paid-description file I/O must run outside the SQL transaction'
            return value
        return operation

    repository.transaction.side_effect = transaction
    repository.canonical_album.side_effect = lambda identity: identity
    repository.request_for.return_value = None
    repository.request.return_value = None
    repository.legacy_usage.return_value = None
    repository.active_request.return_value = None
    repository.usage.return_value = 0
    repository.access_rows.return_value = []
    repository.access_state.return_value = None
    repository.reserve.return_value = 7
    repository.mark_sent.return_value = True
    return AlbumDescriptions(repository,
        Mock(side_effect=outside_transaction(json.dumps(ready_record()))),
        Mock(side_effect=outside_transaction(('result-file', 'result-digest', 90))),
        Mock(side_effect=outside_transaction(('policy-file', 'policy-digest', 40))))


def ready_record():
    return {'status': 'ready', 'description': 'Saved paid copy.', 'model': 'exact-model',
            'usage': {'total_tokens': 10}}


def request(**values):
    return {'id': 7, 'status': 'sent', 'claim_token': 'owner', 'lease_until': 160,
            'attempt_count': 2, 'credential_key': 'credential', 'retry_at': 0,
            'outcome_filename': 'result-file', 'outcome_digest': 'result-digest', **values}


@pytest.mark.parametrize('album,expected', [
    ({'browse_id': 'release'}, 3), ({'album_id': 2}, 3),
    ({'album_id': 2, 'browse_id': 'release'}, 3),
    ({'album_id': 9, 'browse_id': 'release'}, None),
    ({'album_id': True}, None), ({'album_id': '2'}, None),
    ({'album_id': 9}, None), ({'artist': 'Artist', 'title': 'Title'}, None),
])
def test_only_explicit_and_agreeing_release_evidence_identifies_an_album(descriptions, album, expected):
    descriptions.repository.album_by_browse_id.return_value = 3
    descriptions.repository.album_by_id.side_effect = lambda identity: 3 if identity == 2 else None
    assert descriptions.album_id(album) == expected


def test_legacy_summary_composes_proposals_without_paid_request_sql(descriptions):
    descriptions.installed = False
    descriptions.proposals = Mock()
    descriptions.proposals.get_proposals.side_effect = [
        {'paid': {'description': {'kind': 'generated'}}, 'editorial': {'description': {'kind': 'editorial'}},
         'missing': {}}, {'requested': True}]
    assert descriptions.summary() == (1, True)
    descriptions.repository.summary.assert_not_called()


def test_reservation_counts_the_attempt_before_transport_and_supplies_policy_reference(descriptions):
    result = descriptions.claim(3, {'model': 'exact-model'}, 'credential', 20, 86401)
    assert result['status'] == 'reserved' and result['request_id'] == 7 and result['attempt'] == 1
    arguments = descriptions.repository.reserve.call_args
    assert arguments.args[3:6] == (None, 1, result['token'])
    assert arguments.args[6:] == ('credential', 86461, 1, 86401)
    assert arguments.kwargs == {'initial_status': 'pending', 'claimed_status': 'claimed',
                               'policy_reference': ('policy-file', 'policy-digest', 40)}


def test_saved_success_is_read_after_sql_commit_even_when_the_limit_is_zero(descriptions):
    descriptions.repository.request_for.return_value = 7
    descriptions.repository.request.return_value = request(status='ready')
    assert descriptions.claim(3, {}, 'new-key', 0, 100) == {'status': 'cached', 'record': ready_record()}
    descriptions.repository.reserve.assert_not_called()
    descriptions.read_result.assert_called_once_with('result-file', 'result-digest')


def test_current_paid_claim_blocks_other_albums_without_spending_again(descriptions):
    descriptions.repository.active_request.return_value = {'id': 8, 'status': 'sent', 'lease_until': 150}
    assert descriptions.claim(3, {}, 'credential', 20, 100) == {'status': 'busy', 'retry_at': 150}
    descriptions.repository.reserve.assert_not_called()


def test_expired_authorized_send_stays_uncertain_and_keeps_its_owner_token(descriptions):
    descriptions.repository.request_for.return_value = 7
    descriptions.repository.request.side_effect = [request(), request(status='uncertain')]
    descriptions.repository.active_request.return_value = {'id': 7, 'status': 'sent', 'lease_until': 100}
    assert descriptions.claim(3, {}, 'credential', 20, 161) == {'status': 'uncertain'}
    descriptions.repository.expire_request.assert_called_once_with(7, 'uncertain', True, 161)
    descriptions.repository.reserve.assert_not_called()


def test_unsent_expiry_replaces_the_token_and_preserves_attempt_count(descriptions):
    descriptions.repository.request_for.return_value = 7
    descriptions.repository.request.side_effect = [request(status='claimed'), request(status='paused')]
    descriptions.repository.active_request.return_value = {'id': 7, 'status': 'claimed', 'lease_until': 100}
    result = descriptions.claim(3, {}, 'credential', 20, 161)
    assert result['status'] == 'reserved' and result['attempt'] == 3 and result['token'] != 'owner'
    descriptions.repository.expire_request.assert_called_once_with(7, 'paused', False, 161)


@pytest.mark.parametrize('legacy,expected', [
    ({'lease_until': 110, 'claim_token': None, 'send_authorized': False, 'status': 'paused'},
     {'status': 'busy', 'retry_at': 110}),
    ({'lease_until': 0, 'claim_token': 'legacy', 'send_authorized': True, 'status': 'sent'},
     {'status': 'uncertain'}),
])
def test_legacy_usage_does_not_acquire_an_invented_album_owner(descriptions, legacy, expected):
    descriptions.repository.legacy_usage.return_value = legacy
    assert descriptions.claim(3, {}, 'credential', 20, 100) == expected
    descriptions.repository.reserve.assert_not_called()


def test_daily_limit_is_scoped_to_utc_day_and_returns_the_next_boundary(descriptions):
    descriptions.repository.usage.return_value = 5
    assert descriptions.claim(3, {}, 'credential', 5, 86401) == {'status': 'daily_limit', 'retry_at': 172800}
    descriptions.repository.usage.assert_called_once_with(1)
    descriptions.repository.reserve.assert_not_called()


def test_auth_block_takes_precedence_over_a_longer_host_cooldown(descriptions):
    descriptions.repository.access_rows.return_value = [
        {'status': 'unavailable', 'retry_at': 9000}, {'status': 'auth', 'retry_at': 0}]
    assert descriptions.claim(3, {}, 'credential', 5, 100) == {'status': 'auth', 'retry_at': 0}
    descriptions.repository.reserve.assert_not_called()


def test_unknown_result_is_a_negative_cache_until_its_retry_time(descriptions):
    descriptions.repository.request_for.return_value = 7
    descriptions.repository.request.return_value = request(status='unknown', retry_at=200)
    descriptions.read_result.side_effect = lambda *args: '{"status":"unknown"}'
    assert descriptions.claim(3, {}, 'credential', 5, 100) == {'status': 'cached', 'record': {'status': 'unknown'}}
    descriptions.repository.reserve.assert_not_called()


@pytest.mark.parametrize('changes,counted,count,limit,expected', [
    ({'status': 'claimed'}, (0, 1), 1, 1, True),
    ({'status': 'claimed'}, (1, 1), 1, 1, False),
    ({'status': 'claimed'}, (0, 1), 2, 1, False),
    ({'status': 'sent'}, (0, 1), 1, 1, False),
    ({'status': 'claimed', 'claim_token': 'stale'}, (0, 1), 1, 1, False),
    ({'status': 'claimed', 'lease_until': 100}, (0, 1), 1, 1, False),
])
def test_authorization_requires_this_days_counted_current_claim(descriptions, changes, counted, count, limit, expected):
    descriptions.repository.request.return_value = request(**changes)
    descriptions.repository.counted_attempt.return_value = counted
    descriptions.repository.usage.return_value = count
    assert descriptions.authorize(7, 'owner', 'credential', limit, 100) is expected
    assert descriptions.repository.mark_sent.called is expected


def test_completion_publishes_before_receipting_and_records_usage_once(descriptions):
    descriptions.repository.request.return_value = request()
    assert descriptions.finish(7, 'owner', ready_record(), 100)
    arguments = descriptions.repository.complete.call_args
    assert arguments.args == (7, 2, 'owner', {'status': 'ready', 'token': None, 'retry_at': 0, 'access': None},
        ('result-file', 'result-digest', 90), {'total_tokens': 10}, 0, 100)
    assert arguments.kwargs == {'model': 'exact-model'}
    assert descriptions.repository.request.call_count == 2


def test_stale_owner_cannot_publish_a_result(descriptions):
    descriptions.repository.request.return_value = request(claim_token='new-owner')
    assert not descriptions.finish(7, 'owner', ready_record(), 100)
    descriptions.publish_result.assert_not_called()
    descriptions.repository.complete.assert_not_called()


def test_claim_is_rechecked_after_file_publication(descriptions):
    descriptions.repository.request.side_effect = [request(), request(claim_token='new-owner')]
    assert not descriptions.finish(7, 'owner', ready_record(), 100)
    descriptions.publish_result.assert_called_once()
    descriptions.repository.complete.assert_not_called()


def test_failed_file_publication_leaves_request_and_usage_untouched(descriptions):
    descriptions.repository.request.return_value = request()
    descriptions.publish_result.side_effect = OSError('disk unavailable')
    with pytest.raises(OSError, match='disk unavailable'):
        descriptions.finish(7, 'owner', ready_record(), 100)
    descriptions.repository.transaction.assert_not_called()
    descriptions.repository.complete.assert_not_called()


def test_unsent_claim_cannot_accept_paid_output(descriptions):
    descriptions.repository.request.return_value = request(status='claimed')
    with pytest.raises(ValueError, match='authorized transport'):
        descriptions.finish(7, 'owner', ready_record(), 100)
    descriptions.publish_result.assert_not_called()


def test_unavailable_send_remains_uncertain_and_blocks_the_host(descriptions):
    descriptions.repository.request.return_value = request()
    assert descriptions.finish(7, 'owner', {'status': 'unavailable', 'retry_at': 300}, 100)
    decision = descriptions.repository.complete.call_args.args[3]
    assert decision == {'status': 'uncertain', 'token': 'owner', 'retry_at': 300,
        'access': {'scope': 'host', 'credential': '', 'status': 'unavailable', 'retry_at': 300, 'updated_at': 100}}


def test_later_failure_cannot_clear_a_sticky_credential_block(descriptions):
    descriptions.repository.request.return_value = request()
    descriptions.repository.access_state.return_value = {'status': 'auth', 'retry_at': 900, 'updated_at': 101}
    assert descriptions.finish(7, 'owner', {'status': 'rate_limit', 'retry_at': 200}, 100)
    access = descriptions.repository.complete.call_args.args[3]['access']
    assert access == {'scope': 'credential', 'credential': 'credential', 'status': 'auth',
                      'retry_at': 900, 'updated_at': 101}
