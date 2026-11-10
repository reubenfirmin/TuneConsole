"""Account setup orders independent owners without opening SQLite."""
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.accounts import Accounts
from yt_playlist.core import runtime as runtime_module
from yt_playlist.core.config import load_accounts

pytestmark = pytest.mark.usefixtures('no_database')


def test_setup_validates_clients_then_commits_bindings_then_publishes_config(tmp_path, monkeypatch):
    accounts = create_autospec(Accounts, instance=True, spec_set=True)
    runtime = runtime_module.Runtime(SimpleNamespace(accounts=accounts), tmp_path / 'config.toml', tmp_path)
    order = []
    original_replace = runtime_module.os.replace

    def build_client(config, bridge):
        order.append('validate')
        return object()

    def bind(configs):
        assert order == ['validate']
        assert not runtime.configured and not runtime.config_path.exists()
        order.append('bindings')
        return {'Main': 42}

    def publish(staged, destination):
        assert order == ['validate', 'bindings']
        assert load_accounts(staged)[0].account_id == 42
        assert not runtime.configured
        order.append('config')
        original_replace(staged, destination)

    monkeypatch.setattr(runtime_module, 'build_client', build_client)
    monkeypatch.setattr(runtime_module.os, 'replace', publish)
    accounts.replace_accounts.side_effect = bind
    runtime.apply_setup([dict(label='Main', credential_ref='bridge', is_master=True)])
    assert order == ['validate', 'bindings', 'config']
    assert runtime.configured and set(runtime.clients()) == {42}
    assert list(tmp_path.glob('.config.toml.*')) == []


def test_client_validation_failure_does_not_commit_bindings_or_publish_a_config(tmp_path, monkeypatch):
    accounts = create_autospec(Accounts, instance=True, spec_set=True)
    runtime = runtime_module.Runtime(SimpleNamespace(accounts=accounts), tmp_path / 'config.toml', tmp_path)
    monkeypatch.setattr(runtime_module, 'build_client', Mock(side_effect=ValueError('invalid client')))
    with pytest.raises(ValueError, match='not saved'):
        runtime.apply_setup([dict(label='Main', credential_ref='bridge', is_master=True)])
    accounts.replace_accounts.assert_not_called()
    assert not runtime.configured and not runtime.config_path.exists()
    assert list(tmp_path.glob('.config.toml.*')) == []
