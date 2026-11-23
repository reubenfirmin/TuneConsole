"""Authentication notice coordination uses only the account command capability."""
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.auth_sessions import AuthSessions

pytestmark = pytest.mark.usefixtures('no_database')


def test_confirmation_reconciles_other_account_changes_into_the_existing_cache():
    accounts = Mock(spec_set=['auth_expirations', 'update_auth_expiration'])
    accounts.auth_expirations.return_value = {1: 'main'}
    accounts.update_auth_expiration.return_value = {2: 'brand', 3: 'other'}
    current = {}
    sessions = AuthSessions(accounts, current, Mock(spec_set=['warning']))
    sessions.load()
    assert current == {1: 'main'}
    sessions.flag(2, 'brand')
    assert current == {2: 'brand', 3: 'other'}
    accounts.update_auth_expiration.assert_called_once_with(account_id=2, label='brand')


def test_clear_after_write_pause_retries_even_when_the_cache_is_already_empty():
    accounts = Mock(spec_set=['auth_expirations', 'update_auth_expiration'])
    accounts.update_auth_expiration.side_effect = [None, {}]
    current = {1: 'main'}
    sessions = AuthSessions(accounts, current, Mock(spec_set=['warning']))
    sessions.clear_all()
    assert current == {}
    sessions.clear_all()
    assert accounts.update_auth_expiration.call_count == 2
    assert all(call.kwargs == {'clear_all': True}
               for call in accounts.update_auth_expiration.call_args_list)


def test_unreadable_saved_notice_keeps_live_state_and_logs_without_transport_errors():
    accounts = Mock(spec_set=['auth_expirations', 'update_auth_expiration'])
    accounts.auth_expirations.side_effect = ValueError('invalid saved notice')
    accounts.update_auth_expiration.return_value = {2: 'brand'}
    current, logger = {1: 'live'}, Mock(spec_set=['warning'])
    sessions = AuthSessions(accounts, current, logger)
    sessions.load()
    assert current == {1: 'live'}
    sessions.clear(1)
    assert current == {2: 'brand'}
    accounts.update_auth_expiration.assert_called_once_with(account_id=1)
    logger.warning.assert_called_once()
