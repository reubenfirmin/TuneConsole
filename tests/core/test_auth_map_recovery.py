"""Authentication flags reject invalid owners and preserve independent accounts."""

import pytest

from tests.core.test_collection_owner_writes import context
from tests.core.test_predecessor_writes import installed


@pytest.mark.parametrize('installed', [107], indirect=True)
def test_valid_mutations_follow_rejected_authentication_owners(installed):
    store, _ = installed
    account = store.accounts.upsert_account('Current', None, True)
    current = context(store)
    for owner, label in [('bad', 'Old'), (0, 'Zero'), (True, 'Aliased'),
                         (account, []), (999, 'Unknown')]:
        with pytest.raises(ValueError):
            store.accounts.repository.flag_auth_expiration(owner, label)
        assert current.auth_expired == {}
        current.flag_auth_expired(account, 'Current')
        assert context(store).auth_expired == {account: 'Current'}
        current.clear_auth_expired(account)
        assert context(store).auth_expired == {}
        assert not store.conn.in_transaction


@pytest.mark.parametrize('installed', [107], indirect=True)
def test_clear_operations_preserve_other_authentication_owners(installed):
    store, _ = installed
    account = store.accounts.upsert_account('Current', None, True)
    other = store.accounts.upsert_account('Other', 'other', False)
    current = context(store)
    current.flag_auth_expired(other, 'Other')
    current.flag_auth_expired(account, 'Current')
    current.clear_auth_expired(account)
    assert context(store).auth_expired == {other: 'Other'}
    current.clear_auth_expired(account)
    assert context(store).auth_expired == {other: 'Other'}
    current.clear_all_auth_expired()
    assert context(store).auth_expired == {}
    assert not store.conn.in_transaction
