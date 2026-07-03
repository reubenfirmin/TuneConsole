import pytest
from yt_playlist.core.runtime import Runtime


def _stub_ytmusic(monkeypatch):
    import yt_playlist.core.identities as identities
    monkeypatch.setattr(identities, "YTMusic", lambda *a, **k: object())        # build_client provider


def test_load_provisions_default_identity_on_first_run(store, monkeypatch, tmp_path):
    # First run (no config yet): load() seeds a usable "main" identity so the app works
    # immediately; the /setup wizard is only for multi-identity users now.
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.load()
    assert rt.configured is True
    assert (tmp_path / "config.toml").exists()                 # default config written
    assert [i.label for i in store.get_identities()] == ["main"]
    assert len(rt.clients()) == 1


def test_load_unconfigured_when_config_unusable(store, tmp_path):
    # A broken config leaves the runtime unconfigured (setup shown) rather than crashing,
    # and the client provider degrades to {} instead of raising.
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not [[ valid toml")
    rt = Runtime(store, cfg, tmp_path)
    rt.load()
    assert rt.configured is False
    assert rt.clients() == {}


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


def test_apply_setup_reloads_over_provisioned_default(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.load()
    assert rt.configured is True and len(rt.clients()) == 1    # auto-provisioned "main"
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9", "is_master": False},
    ])
    assert rt.configured is True
    assert len(rt.clients()) == 2   # load() ran inside apply_setup and swapped the provider in
