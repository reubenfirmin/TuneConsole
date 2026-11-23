"""Account decisions use explicit repository facts without a database."""
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.accounts import Accounts
from yt_playlist.core.config import AccountConfig
from yt_playlist.repos.accounts import AccountRepo

pytestmark = pytest.mark.usefixtures('no_database')


def row(identity=1, label='Main', brand=None, master=True, active=True):
    return dict(id=identity, label=label, brand_account_id=brand,
                is_master=int(master), active=int(active))


@pytest.fixture
def accounts():
    repository = create_autospec(AccountRepo, instance=True, spec_set=True)
    repository.account_rows.return_value = [row()]
    repository.account_by_label.return_value = None
    return Accounts(repository, paused=Mock(return_value=False))


def test_explicit_identity_allows_label_changes(accounts):
    accounts.repository.replace_accounts.return_value = {'Renamed': 1}
    config = AccountConfig('Renamed', None, True, 1)
    assert accounts.replace_accounts([config]) == {'Renamed': 1}
    accounts.repository.replace_accounts.assert_called_once_with(
        [(1, 'Renamed', None, 1)], expected=[row()], importing=False)


@pytest.mark.parametrize('config,message', [
    (AccountConfig('Main', None, True, 99), 'Unknown identity'),
    (AccountConfig('Main', 'another-owner', True, 1), 'cannot be retargeted'),
    (AccountConfig('Main', None, False, 1), 'exactly one'),
    (AccountConfig('Main', None, 2, 1), 'boolean or integer'),
])
def test_invalid_configuration_never_reaches_the_writer(accounts, config, message):
    with pytest.raises(ValueError, match=message):
        accounts.replace_accounts([config])
    accounts.repository.replace_accounts.assert_not_called()


def test_label_match_precedes_ambiguous_brand_bindings(accounts):
    accounts.repository.account_rows.return_value = [row(), row(2, 'Other', master=False)]
    accounts.replace_accounts([AccountConfig('Main', None, True)])
    assert accounts.repository.replace_accounts.call_args.args[0] == [(1, 'Main', None, 1)]


def test_unique_brand_match_recovers_a_renamed_identity(accounts):
    accounts.repository.account_rows.return_value = [row(brand='owner')]
    accounts.replace_accounts([AccountConfig('Renamed', 'owner', True)])
    assert accounts.repository.replace_accounts.call_args.args[0][0][0] == 1


def test_ambiguous_brand_bindings_do_not_choose_an_arbitrary_old_account(accounts):
    accounts.repository.account_rows.return_value = [row(), row(2, 'Other', master=False)]
    accounts.replace_accounts([AccountConfig('New', None, True)])
    assert accounts.repository.replace_accounts.call_args.args[0][0][0] is None


def test_reusing_a_label_for_another_remote_owner_creates_a_new_local_identity(accounts):
    accounts.replace_accounts([AccountConfig('Main', 'different-owner', True)])
    assert accounts.repository.replace_accounts.call_args.args[0][0][0] is None


def test_implicit_and_explicit_selections_cannot_reuse_one_identity(accounts):
    with pytest.raises(ValueError, match='only appear once'):
        accounts.replace_accounts([AccountConfig('Main', None, True),
                                   AccountConfig('Renamed', None, False, 1)])
    accounts.repository.replace_accounts.assert_not_called()


@pytest.mark.parametrize('flag', [2, -1, 1.0, 'false', None])
def test_invalid_master_flag_is_rejected_before_upsert(accounts, flag):
    with pytest.raises(ValueError, match='boolean or integer'):
        accounts.upsert_account('Main', None, flag)
    accounts.repository.upsert_account.assert_not_called()


def test_upsert_cannot_retarget_an_existing_label(accounts):
    accounts.repository.account_by_label.return_value = row(brand='old-owner')
    with pytest.raises(ValueError, match='cannot be retargeted'):
        accounts.upsert_account('Main', 'new-owner', True)
    accounts.repository.upsert_account.assert_not_called()


def test_account_reads_use_the_repository_during_maintenance(accounts):
    rows = [SimpleNamespace(id=1, is_master=True)]
    accounts.repository.get_accounts.return_value = rows
    accounts.paused.return_value = True
    assert accounts.get_accounts() == rows
    assert accounts.get_master_account() is rows[0]


def test_missing_master_is_explicit(accounts):
    accounts.repository.get_accounts.return_value = []
    with pytest.raises(ValueError, match='No master'):
        accounts.get_master_account()


def test_native_authentication_flag_preserves_the_repository_current_state(accounts):
    accounts.repository.flag_auth_expiration.return_value = {1: 'Main', 2: 'Other'}
    assert accounts.update_auth_expiration(account_id=2, label='Other') == {1: 'Main', 2: 'Other'}
    accounts.repository.flag_auth_expiration.assert_called_once_with(2, 'Other')


@pytest.mark.parametrize('clear_all,expected', [(False, 2), (True, None)])
def test_native_authentication_clear_targets_one_or_all_accounts(accounts, clear_all, expected):
    accounts.repository.clear_auth_expirations.return_value = {}
    assert accounts.update_auth_expiration(account_id=2, clear_all=clear_all) == {}
    accounts.repository.clear_auth_expirations.assert_called_once_with(expected)


@pytest.mark.parametrize('paused_values', [[True], [False, True]])
def test_paused_authentication_updates_do_not_touch_storage(accounts, paused_values):
    accounts.paused.side_effect = paused_values
    assert accounts.update_auth_expiration(account_id=1, label='Main') is None
    accounts.repository.flag_auth_expiration.assert_not_called()
    accounts.repository.clear_auth_expirations.assert_not_called()


def test_native_write_rejected_during_maintenance_does_not_claim_success(accounts):
    accounts.repository.flag_auth_expiration.return_value = None
    assert accounts.update_auth_expiration(account_id=1, label='Main') is None
