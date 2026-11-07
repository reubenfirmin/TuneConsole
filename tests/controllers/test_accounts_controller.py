"""Account decisions use explicit repository facts without a database."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.accounts import Accounts
from yt_playlist.core.config import AccountConfig
from yt_playlist.repos.accounts import AccountRepo
from yt_playlist.controllers.settings import Settings

pytestmark = pytest.mark.usefixtures('no_database')


def row(identity=1, label='Main', credential='bridge', brand=None, master=True, active=True):
    return dict(id=identity, label=label, credential_ref=credential, brand_account_id=brand,
                is_master=int(master), active=int(active))


@pytest.fixture
def accounts():
    repository = create_autospec(AccountRepo, instance=True, spec_set=True)
    repository.account_rows.return_value = [row()]
    repository.account_by_label.return_value = None
    repository.has_auth_state.return_value = True
    settings = create_autospec(Settings, instance=True, spec_set=True)
    return Accounts(repository, settings, paused=Mock(return_value=False),
                    maintenance_accounts=Mock(return_value=None))


def test_explicit_identity_allows_label_and_credential_changes(accounts):
    accounts.repository.replace_accounts.return_value = {'Renamed': 1}
    config = AccountConfig('Renamed', 'new-credential', None, True, 1)
    assert accounts.replace_accounts([config]) == {'Renamed': 1}
    accounts.repository.replace_accounts.assert_called_once_with(
        [('Renamed', 'new-credential', None, 1, 1)], expected=[row()])


@pytest.mark.parametrize('config,message', [
    (AccountConfig('Main', 'bridge', None, True, 99), 'Unknown identity'),
    (AccountConfig('Main', 'bridge', 'another-owner', True, 1), 'cannot be retargeted'),
    (AccountConfig('Main', 'bridge', None, False, 1), 'exactly one'),
    (AccountConfig('Main', 'bridge', None, 2, 1), 'boolean or integer'),
])
def test_invalid_configuration_never_reaches_the_writer(accounts, config, message):
    with pytest.raises(ValueError, match=message):
        accounts.replace_accounts([config])
    accounts.repository.replace_accounts.assert_not_called()


def test_label_match_precedes_ambiguous_credentials(accounts):
    accounts.repository.account_rows.return_value = [row(), row(2, 'Other', master=False)]
    accounts.replace_accounts([AccountConfig('Main', 'bridge', None, True)])
    assert accounts.repository.replace_accounts.call_args.args[0] == [('Main', 'bridge', None, 1, 1)]


def test_unique_credential_and_owner_match_recovers_a_renamed_identity(accounts):
    accounts.replace_accounts([AccountConfig('Renamed', 'bridge', None, True)])
    assert accounts.repository.replace_accounts.call_args.args[0][0][-1] == 1


def test_ambiguous_credentials_do_not_choose_an_arbitrary_old_account(accounts):
    accounts.repository.account_rows.return_value = [row(), row(2, 'Other', master=False)]
    accounts.replace_accounts([AccountConfig('New', 'bridge', None, True)])
    assert accounts.repository.replace_accounts.call_args.args[0][0][-1] is None


def test_reusing_a_label_for_another_remote_owner_creates_a_new_local_identity(accounts):
    accounts.replace_accounts([AccountConfig('Main', 'bridge', 'different-owner', True)])
    assert accounts.repository.replace_accounts.call_args.args[0][0][-1] is None


def test_implicit_and_explicit_selections_cannot_reuse_one_identity(accounts):
    with pytest.raises(ValueError, match='only appear once'):
        accounts.replace_accounts([AccountConfig('Main', 'bridge', None, True),
                                   AccountConfig('Renamed', 'bridge', None, False, 1)])
    accounts.repository.replace_accounts.assert_not_called()


@pytest.mark.parametrize('configs', [
    [AccountConfig('Main', 'bridge', None, True)],
    [AccountConfig('Renamed', 'bridge', None, True, 1)],
])
def test_maintenance_can_load_existing_ids_without_writing(accounts, configs):
    assert accounts.configured_ids(configs) == {configs[0].label: 1}
    accounts.repository.account_rows.assert_called_once_with(active=True)
    accounts.repository.replace_accounts.assert_not_called()


@pytest.mark.parametrize('configs,existing', [
    ([AccountConfig('New', 'other-credential', None, True)], [row()]),
    ([AccountConfig('Main', 'bridge', None, True)], [row(), row(2, 'Other', master=False)]),
    ([AccountConfig('Main', 'bridge', None, False, 1), AccountConfig('Other', 'bridge', None, True, 2)],
     [row(), row(2, 'Other', master=False)]),
])
def test_maintenance_rejects_new_removed_or_changed_master_accounts(accounts, configs, existing):
    accounts.repository.account_rows.return_value = existing
    with pytest.raises(ValueError, match='must wait'):
        accounts.configured_ids(configs)
    accounts.repository.replace_accounts.assert_not_called()


@pytest.mark.parametrize('flag', [2, -1, 1.0, 'false', None])
def test_invalid_master_flag_is_rejected_before_upsert(accounts, flag):
    with pytest.raises(ValueError, match='boolean or integer'):
        accounts.upsert_account('Main', 'bridge', None, flag)
    accounts.repository.upsert_account.assert_not_called()


def test_upsert_cannot_retarget_an_existing_label(accounts):
    accounts.repository.account_by_label.return_value = row(brand='old-owner')
    with pytest.raises(ValueError, match='cannot be retargeted'):
        accounts.upsert_account('Main', 'bridge', 'new-owner', True)
    accounts.repository.upsert_account.assert_not_called()


def test_maintenance_uses_a_detached_copy_of_cached_accounts(accounts):
    cached = [SimpleNamespace(id=1, is_master=True)]
    accounts.paused.return_value = True
    accounts.maintenance_accounts.return_value = cached
    result = accounts.get_accounts()
    assert result == cached and result is not cached
    assert accounts.get_master_account() is cached[0]
    accounts.repository.get_accounts.assert_not_called()


def test_missing_master_is_explicit(accounts):
    accounts.repository.get_accounts.return_value = []
    with pytest.raises(ValueError, match='No master'):
        accounts.get_master_account()


def test_native_authentication_flag_preserves_the_repository_current_state(accounts):
    accounts.repository.flag_auth_expiration.return_value = {1: 'Main', 2: 'Other'}
    assert accounts.update_auth_expiration(account_id=2, label='Other') == ({1: 'Main', 2: 'Other'}, False)
    accounts.repository.flag_auth_expiration.assert_called_once_with(2, 'Other')
    accounts.settings.set_setting.assert_not_called()


@pytest.mark.parametrize('clear_all,expected', [(False, 2), (True, None)])
def test_native_authentication_clear_targets_one_or_all_accounts(accounts, clear_all, expected):
    accounts.repository.clear_auth_expirations.return_value = {}
    assert accounts.update_auth_expiration(account_id=2, clear_all=clear_all) == ({}, False)
    accounts.repository.clear_auth_expirations.assert_called_once_with(expected)


@pytest.mark.parametrize('paused_values', [[True], [False, True]])
def test_paused_authentication_updates_do_not_touch_storage(accounts, paused_values):
    accounts.paused.side_effect = paused_values
    assert accounts.update_auth_expiration(account_id=1, label='Main') is None
    accounts.repository.has_auth_state.assert_not_called()
    accounts.settings.get_setting.assert_not_called()


@pytest.mark.parametrize('raw,invalid', [('not-json', True), ('[]', True), ('{"01":"bad"}', True),
                                       ('{"1":"Main"}', False)])
def test_legacy_authentication_merge_reports_invalid_predecessors_and_preserves_valid_flags(accounts, raw, invalid):
    accounts.repository.has_auth_state.return_value = False
    accounts.settings.get_setting.return_value = raw
    expected = {2: 'Other'} if invalid else {1: 'Main', 2: 'Other'}
    assert accounts.update_auth_expiration(account_id=2, label='Other') == (expected, invalid)
    key, encoded = accounts.settings.set_setting.call_args.args
    assert key == 'auth_expired_identities'
    assert json.loads(encoded) == {str(key): value for key, value in expected.items()}
    accounts.repository.flag_auth_expiration.assert_not_called()


def test_clearing_the_last_legacy_flag_deletes_the_old_setting(accounts):
    accounts.repository.has_auth_state.return_value = False
    accounts.settings.get_setting.return_value = '{"1":"Main"}'
    assert accounts.update_auth_expiration(account_id=1) == ({}, False)
    accounts.settings.delete_setting.assert_called_once_with('auth_expired_identities')


def test_native_write_rejected_during_maintenance_does_not_claim_success(accounts):
    accounts.repository.flag_auth_expiration.return_value = None
    assert accounts.update_auth_expiration(account_id=1, label='Main') is None
