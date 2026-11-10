"""A rejected compatible authentication map can be rebuilt by normal updates."""
import json

import pytest

from tests.core.test_collection_owner_writes import context
from tests.core.test_predecessor_writes import installed


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_valid_mutations_reseed_rejected_authentication_maps(installed, caplog):
    store, _ = installed
    account = store.accounts.upsert_account('Current', 'current.json', None, True)
    for invalid in ({'not-an-account-id': 'Old'}, [], 'not a map', None,
                    {str(account): []}, {'0': 'Zero'}, {'01': 'Aliased'},
                    {str(2**63): 'Overflow'}, {'999': 'Historical', 'bad': 'Invalid'}):
        store.settings.set_setting('auth_expired_identities', json.dumps(invalid))
        current = context(store)
        assert current.auth_expired == {}
        assert 'ignoring unreadable auth_expired_identities setting' in caplog.text
        current.flag_auth_expired(account, 'Current')
        assert context(store).auth_expired == {account: 'Current'}
        assert json.loads(store.settings.get_setting('auth_expired_identities')) == {str(account): 'Current'}
        current.clear_auth_expired(account)
        assert context(store).auth_expired == {}
        assert store.settings.get_setting('auth_expired_identities') is None
        assert not store.conn.in_transaction
    # A valid historical ID is independent evidence even without a current row.
    store.settings.set_setting('auth_expired_identities', json.dumps({'999': 'Historical'}))
    current = context(store)
    current.flag_auth_expired(account, 'Current')
    assert context(store).auth_expired == {999: 'Historical', account: 'Current'}
    current.clear_auth_expired(account)
    assert context(store).auth_expired == {999: 'Historical'}
    current.clear_all_auth_expired()
    assert context(store).auth_expired == {}


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_clear_operations_discard_rejected_authentication_maps(installed):
    store, _ = installed
    account = store.accounts.upsert_account('Current', 'current.json', None, True)
    current = context(store)
    for clear in (lambda: current.clear_auth_expired(account), current.clear_all_auth_expired):
        store.settings.set_setting('auth_expired_identities', '{"bad":"Old"}')
        clear()
        assert context(store).auth_expired == {}
        assert store.settings.get_setting('auth_expired_identities') is None
        assert not store.conn.in_transaction
