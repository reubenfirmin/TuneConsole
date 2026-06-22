import pytest
from yt_playlist.runtime import Runtime


def _stub_ytmusic(monkeypatch, blob='{"cookie": "x"}'):
    import ytmusicapi
    import yt_playlist.identities as identities
    monkeypatch.setattr(ytmusicapi, "setup", lambda headers_raw=None: blob)
    monkeypatch.setattr(ytmusicapi, "YTMusic", lambda *a, **k: object())       # store_credentials check
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
    rt.apply_setup("Cookie: SID=abc", [
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9", "is_master": False},
    ])
    assert rt.configured is True
    assert (tmp_path / "browser.json").exists()
    assert (tmp_path / "config.toml").exists()
    clients = rt.clients()
    assert len(clients) == 2                       # one client per identity id
    assert len(store.get_identities()) == 2


def test_apply_setup_invalid_identities_raises_before_write(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        rt.apply_setup("Cookie: SID=abc", [
            {"label": "a", "credential_ref": "browser.json", "brand_account_id": None, "is_master": False}])
    assert rt.configured is False
    assert not (tmp_path / "config.toml").exists()  # validated before any write


def test_apply_setup_reuses_existing_credential_when_blank(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    (tmp_path / "browser.json").write_text('{"cookie": "x"}')   # credential already present
    rt.apply_setup("", [
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True}])
    assert rt.configured is True


def test_apply_setup_blank_headers_no_credential_raises(store, tmp_path):
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    with pytest.raises(ValueError, match="no saved credential"):
        rt.apply_setup("", [
            {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True}])


def test_sign_out_deletes_credential_and_unconfigures(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.apply_setup("Cookie: SID=abc", [
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True}])
    assert rt.configured is True
    rt.sign_out()
    assert not (tmp_path / "browser.json").exists()   # local cookie file is gone
    assert (tmp_path / "config.toml").exists()         # identity config is kept
    assert rt.configured is False                      # no credential -> back to setup
    with pytest.raises(RuntimeError):
        rt.clients()


def test_sign_out_when_no_credential_is_noop(store, tmp_path):
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.sign_out()                                      # missing_ok -> no error
    assert rt.configured is False


def test_load_reloads_after_external_config(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.load()
    assert rt.configured is False
    rt.apply_setup("Cookie: SID=abc", [
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True}])
    assert rt.configured is True   # load() ran inside apply_setup and swapped the provider in
