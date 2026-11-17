"""Live identity validation uses narrow ports without SQLite or web context."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.identity_checks import IdentityCheck, IdentityChecks
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.core.bridge import BridgeError

pytestmark = pytest.mark.usefixtures('no_database')


def identity(account_id, brand=None):
    return SimpleNamespace(id=account_id, label=f'identity {account_id}', brand_account_id=brand)


def client():
    result = Mock(spec_set=['get_account_info', 'get_library_playlists'])
    result.get_account_info.return_value = {'accountName': 'Signed in'}
    result.get_library_playlists.return_value = []
    return result


def checks(identities, clients):
    accounts = Mock(spec_set=['get_accounts'])
    accounts.get_accounts.return_value = identities
    return IdentityChecks(accounts, Mock(return_value=clients), Mock(),
                          Mock(spec_set=['exception', 'debug']))


def test_empty_library_authenticates_only_after_both_live_permissions_succeed():
    live, events = client(), []
    live.get_account_info.side_effect = lambda: events.append('account') or {'accountName': 'main'}
    live.get_library_playlists.side_effect = lambda **kwargs: events.append(('library', kwargs)) or []
    command = checks([identity(1)], {1: live})
    command.clear_authentication.side_effect = lambda account_id: events.append(('clear', account_id))
    assert command.check() == [IdentityCheck(1, 'identity 1', 'authenticated')]
    assert events == ['account', ('library', {'limit': 1}), ('clear', 1)]
    command.accounts.get_accounts.assert_called_once_with()
    command.clients.assert_called_once_with()


@pytest.mark.parametrize('brand', ['http://example.com', 'https://example.com/b/123/',
                                  '@handle', 'UC12345', 'b/123'])
def test_invalid_brand_is_rejected_before_client_acquisition(brand):
    command = checks([identity(2, brand)], {})
    assert command.check() == [IdentityCheck(2, 'identity 2', 'invalid_brand_id')]
    command.clients.assert_not_called()
    command.clear_authentication.assert_not_called()


@pytest.mark.parametrize('selected', [2, 999])
def test_selected_or_missing_identity_never_checks_unrelated_accounts(selected):
    main, brand = client(), client()
    command = checks([identity(1), identity(2, '123456')], {1: main, 2: brand})
    result = command.check(selected)
    assert result == ([IdentityCheck(2, 'identity 2', 'authenticated')] if selected == 2 else [])
    main.get_account_info.assert_not_called()
    if selected == 2:
        brand.get_account_info.assert_called_once_with()
        command.clear_authentication.assert_called_once_with(2)
    else:
        command.clients.assert_not_called()
        command.clear_authentication.assert_not_called()


@pytest.mark.parametrize(('operation', 'response'), [
    ('get_account_info', None), ('get_account_info', []), ('get_account_info', {}),
    ('get_account_info', {'accountName': ''}),
    ('get_library_playlists', None), ('get_library_playlists', {}),
])
def test_unexpected_responses_do_not_clear_expiration_or_block_other_identity(operation, response):
    broken, healthy = client(), client()
    getattr(broken, operation).return_value = response
    command = checks([identity(1), identity(2)], {1: broken, 2: healthy})
    assert [row.outcome for row in command.check()] == ['unexpected_response', 'authenticated']
    command.clear_authentication.assert_called_once_with(2)
    if operation == 'get_account_info':
        broken.get_library_playlists.assert_not_called()


@pytest.mark.parametrize(('brand', 'operation', 'error', 'expected'), [
    (None, 'get_account_info', BridgeError('disconnected'), 'disconnected'),
    ('123', 'get_library_playlists', BridgeError('disconnected'), 'disconnected'),
    (None, 'get_account_info', RuntimeError('HTTP 401'), 'auth_required'),
    (None, 'get_library_playlists', RuntimeError('HTTP 403'), 'auth_required'),
    (None, 'get_account_info', RuntimeError('signInEndpoint'), 'auth_required'),
    (None, 'get_library_playlists', LibrarySnapshotError('playlists', 'signed out', {}, 1),
     'auth_required'),
    ('123', 'get_account_info', RuntimeError('HTTP 400'), 'brand_access_denied'),
    ('123', 'get_library_playlists', RuntimeError('HTTP 404'), 'brand_access_denied'),
    ('123', 'get_library_playlists', RuntimeError('HTTP 403'), 'brand_access_denied'),
    (None, 'get_account_info', RuntimeError('HTTP 400'), 'unexpected_response'),
    ('123', 'get_library_playlists', RuntimeError('provider failed'), 'unexpected_response'),
])
def test_failed_live_permission_keeps_expiration_and_isolates_each_identity(brand, operation, error, expected):
    broken, healthy = client(), client()
    getattr(broken, operation).side_effect = error
    command = checks([identity(1, brand), identity(2)], {1: broken, 2: healthy})
    assert [row.outcome for row in command.check()] == [expected, 'authenticated']
    command.clear_authentication.assert_called_once_with(2)


@pytest.mark.parametrize('failure', [False, True])
def test_missing_clients_remain_unverified_and_client_acquisition_failure_is_logged(failure):
    command = checks([identity(1), identity(2, '@bad')], None)
    if failure:
        command.clients.side_effect = RuntimeError('client initialization failed')
    assert [row.outcome for row in command.check()] == ['unavailable', 'invalid_brand_id']
    command.clear_authentication.assert_not_called()
    assert command.logger.exception.call_count == int(failure)
