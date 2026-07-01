import pytest
from yt_playlist.core.runtime import Runtime


def _stub_ytmusic(monkeypatch):
    import yt_playlist.core.identities as identities
    monkeypatch.setattr(identities, "YTMusic", lambda *a, **k: object())        # build_client provider


def test_load_unconfigured_when_no_config(store, tmp_path):
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.load()
    assert rt.configured is False
    with pytest.raises(RuntimeError):
        rt.clients()


def test_apply_setup_configures_and_builds_provider(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9", "is_master": False},
    ])
    assert rt.configured is True
    assert (tmp_path / "config.toml").exists()
    clients = rt.clients()
    assert len(clients) == 2                       # one client per identity id
    assert len(store.get_identities()) == 2


def test_apply_setup_invalid_identities_raises_before_write(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        rt.apply_setup([
            {"label": "a", "credential_ref": "browser.json", "brand_account_id": None, "is_master": False}])
    assert rt.configured is False
    assert not (tmp_path / "config.toml").exists()  # validated before any write


def test_apply_setup_configures_identity_independent_of_credential(store, monkeypatch, tmp_path):
    # Identity definition is independent of the credential now: the bridge is paired separately
    # and live, so saving identities never needs to wait on (or require) a credential file.
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True}])
    assert rt.configured is True


def test_load_reloads_after_external_config(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.load()
    assert rt.configured is False
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True}])
    assert rt.configured is True   # load() ran inside apply_setup and swapped the provider in
