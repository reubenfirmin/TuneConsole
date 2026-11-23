import pytest
from yt_playlist.core.runtime import Runtime


def _stub_ytmusic(monkeypatch):
    import yt_playlist.core.runtime as runtime
    monkeypatch.setattr(runtime, 'build_client', lambda *a, **k: object())


def test_load_provisions_default_identity_on_first_run(store, monkeypatch, tmp_path):
    # First run (no config yet): load() seeds a usable "main" identity so the app works
    # immediately; the /setup wizard is only for multi-identity users now.
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml")
    assert not store.accounts.bindings_saved()
    rt.load()
    assert rt.configured is True
    assert not (tmp_path / "config.toml").exists()
    assert [i.label for i in store.accounts.get_accounts()] == ["main"]
    assert store.accounts.bindings_saved()
    assert len(rt.clients()) == 1


def test_load_unconfigured_when_config_unusable(store, tmp_path):
    # A broken config leaves the runtime unconfigured (setup shown) rather than crashing,
    # and the client provider degrades to {} instead of raising.
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not [[ valid toml")
    rt = Runtime(store, cfg)
    rt.load()
    assert rt.configured is False
    assert rt.clients() == {}


def test_apply_setup_configures_and_builds_provider(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml")
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9", "is_master": False},
    ])
    assert rt.configured is True
    assert not (tmp_path / "config.toml").exists()
    clients = rt.clients()
    assert len(clients) == 2                       # one client per identity id
    assert len(store.accounts.get_accounts()) == 2


def test_apply_setup_invalid_identities_raises_before_write(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml")
    with pytest.raises(ValueError, match="exactly one"):
        rt.apply_setup([
            {"label": "a", "credential_ref": "browser.json", "brand_account_id": None, "is_master": False}])
    assert rt.configured is False
    assert not (tmp_path / "config.toml").exists()  # validated before any write


def test_apply_setup_configures_identity_independent_of_credential(store, monkeypatch, tmp_path):
    # Account definition is independent of the credential now: the bridge is paired separately
    # and live, so saving identities never needs to wait on (or require) a credential file.
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml")
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True}])
    assert rt.configured is True


def test_apply_setup_reloads_over_provisioned_default(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml")
    rt.load()
    assert rt.configured is True and len(rt.clients()) == 1    # auto-provisioned "main"
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "brand_account_id": None, "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9", "is_master": False},
    ])
    assert rt.configured is True
    assert len(rt.clients()) == 2   # load() ran inside apply_setup and swapped the provider in


def test_apply_setup_removes_identity_from_active_accounts_and_preserves_history(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml")
    main = {"label": "main", "credential_ref": "browser.json", "is_master": True}
    brand = {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9"}
    rt.apply_setup([main, brand])
    ids = {i.label: i.id for i in store.accounts.get_accounts()}
    store.history.record_history_plays(ids["brand"], 1.0, ["song|artist"], source_timezone="UTC")
    playlist = store.playlists.upsert_playlist(ids["brand"], "PL1", "Brand mix", 0, "h", 1.0)

    rt.apply_setup([main])

    assert [(i.id, i.label) for i in store.accounts.get_accounts()] == [(ids["main"], "main")]
    assert set(rt.clients()) == {ids["main"]}
    assert store.history.listening_day_counts(ids["brand"]) == {"song|artist": 1}
    assert store.history.listening_day_counts(ids["main"]) == {}
    assert store.playlists.get_playlist(playlist).account_id == ids["brand"]
    assert list(store.conn.execute("PRAGMA foreign_key_check")) == []

    rt.apply_setup([main, brand])
    assert {i.label: i.id for i in store.accounts.get_accounts()} == ids
    assert set(rt.clients()) == set(ids.values())


def test_accounts_import_once_and_setup_uses_sqlite_after_restart(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    main = store.accounts.upsert_account('main', None, True)
    brand = store.accounts.upsert_account('Previous brand', 'UC9', False)
    removed = store.accounts.upsert_account('Previously removed', 'UC8', False)
    store.conn.execute('UPDATE accounts SET active=0,auth_expired=1 WHERE id=?', (removed,))
    store.settings.set_setting('bridge_paired', '1')
    config = tmp_path / 'config.toml'
    content = ('[providers.musicbrainz]\ncontact="example@test.invalid"\n'
               f'[[identity]]\nid={main}\nlabel="New main"\ncredential_ref="ignored"\n'
               '[[identity]]\nlabel="New brand"\nbrand_account_id="UC9"\nis_master=true\n')
    config.write_text(content)
    rt = Runtime(store, config)
    rt.load()

    assert [(i.id, i.label) for i in store.accounts.get_accounts()] == [(main, 'New main'), (brand, 'New brand')]
    assert store.accounts.get_master_account().id == brand
    assert tuple(store.conn.execute('SELECT active,auth_expired FROM accounts WHERE id=?', (removed,)).fetchone()) == (0, 1)
    assert rt.credentials_present and set(rt.clients()) == {main, brand}
    assert store.accounts.bindings_saved()
    assert store.settings.get_setting('account_bindings_imported') is None
    config.write_text('[[identity]]\nlabel="Changed on disk"\nis_master=true\n')
    Runtime(store, config).load()
    assert [(i.id, i.label) for i in store.accounts.get_accounts()] == [(main, 'New main'), (brand, 'New brand')]
    config.write_text(content)
    rt.apply_setup([{'id': main, 'label': 'Saved in setup', 'is_master': True}])
    assert config.read_text() == content
    restarted = Runtime(store, config)
    restarted.load()
    assert set(restarted.clients()) == {main}
    assert restarted.credentials_present
    assert store.accounts.get_master_account().label == 'Saved in setup'


def test_import_keeps_explicit_ids_when_the_database_is_new(store, tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[[identity]]\nlabel="Main"\nis_master=true\n'
                      '[[identity]]\nid=1\nlabel="Brand"\nbrand_account_id="UC9"\n')
    rt = Runtime(store, config)
    rt.load()
    assert [(i.id, i.label) for i in store.accounts.get_accounts()] == [(1, 'Brand'), (2, 'Main')]


def test_apply_setup_invalid_edit_keeps_existing_identities(store, monkeypatch, tmp_path):
    _stub_ytmusic(monkeypatch)
    rt = Runtime(store, tmp_path / "config.toml")
    rt.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9"},
    ])
    before = store.accounts.get_accounts()
    with pytest.raises(ValueError, match="exactly one"):
        rt.apply_setup([{"label": "main", "credential_ref": "browser.json"}])
    assert store.accounts.get_accounts() == before
    assert set(rt.clients()) == {i.id for i in before}
