"""Account setup orders independent owners without opening SQLite."""
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.accounts import Accounts
from yt_playlist.core import runtime as runtime_module

pytestmark = pytest.mark.usefixtures('no_database')


def test_setup_validates_clients_then_commits_bindings(tmp_path, monkeypatch):
    accounts = create_autospec(Accounts, instance=True, spec_set=True)
    runtime = runtime_module.Runtime(SimpleNamespace(accounts=accounts), tmp_path / 'config.toml')
    order = []

    def build_client(config, bridge):
        order.append('validate')
        return object()

    def bind(configs):
        assert order == ['validate']
        assert not runtime.configured and not runtime.config_path.exists()
        order.append('bindings')
        return {'Main': 42}

    monkeypatch.setattr(runtime_module, 'build_client', build_client)
    accounts.replace_accounts.side_effect = bind
    runtime.apply_setup([dict(label='Main', is_master=True)])
    assert order == ['validate', 'bindings']
    assert runtime.configured and set(runtime.clients()) == {42}
    assert not runtime.config_path.exists()


def test_client_validation_failure_does_not_commit_bindings_or_publish_a_config(tmp_path, monkeypatch):
    accounts = create_autospec(Accounts, instance=True, spec_set=True)
    runtime = runtime_module.Runtime(SimpleNamespace(accounts=accounts), tmp_path / 'config.toml')
    monkeypatch.setattr(runtime_module, 'build_client', Mock(side_effect=ValueError('invalid client')))
    with pytest.raises(ValueError, match='not saved'):
        runtime.apply_setup([dict(label='Main', is_master=True)])
    accounts.replace_accounts.assert_not_called()
    assert not runtime.configured and not runtime.config_path.exists()
