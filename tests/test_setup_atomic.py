from contextlib import contextmanager

import pytest

from yt_playlist.core.runtime import Runtime
from yt_playlist.core import runtime as runtime_mod


def _configured(store, tmp_path):
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.apply_setup([{"label": "main", "credential_ref": "browser.json", "is_master": True},
        {"label": "brand", "credential_ref": "browser.json", "brand_account_id": "UC9"}])
    return rt


@pytest.mark.parametrize("failure", ["publish", "reload", "reconcile", "commit"])
def test_failed_save_retains_file_accounts_and_runtime(store, tmp_path, monkeypatch, failure):
    rt = _configured(store, tmp_path)
    before, provider, accounts = rt.config_path.read_bytes(), rt._provider, store.get_identities()
    if failure == "publish":
        def fail(*args):
            raise OSError("publication failed")
        monkeypatch.setattr(runtime_mod.os, "replace", fail)
    elif failure == "reload":
        def fail(*args):
            raise ValueError("candidate client cannot load")
        monkeypatch.setattr(rt, "_prepare_provider", fail)
    else:
        original = store.identities.reconciled
        @contextmanager
        def failed_reconciliation(configs):
            with original(configs) as ids:
                if failure == "reconcile":
                    raise ValueError("identity reconciliation failed")
                yield ids
                raise OSError("identity commit failed")
        monkeypatch.setattr(store.identities, "reconciled", failed_reconciliation)
    with pytest.raises(ValueError, match="not saved"):
        rt.apply_setup([{"label": "renamed", "credential_ref": "browser.json", "is_master": True}])
    assert rt.config_path.read_bytes() == before
    assert store.get_identities() == accounts
    assert rt.configured and rt._provider is provider
    assert set(rt.clients()) == {i.id for i in accounts}
    assert list(tmp_path.glob(".config.toml.*")) == []


def test_failed_reload_restores_last_usable_file_and_runtime(store, tmp_path):
    rt = _configured(store, tmp_path)
    previous, provider, accounts = rt.config_path.read_bytes(), rt._provider, store.get_identities()
    rt.config_path.write_text("invalid [ toml")
    rt.load()
    assert rt.configured and rt._provider is provider
    assert rt.config_path.read_bytes() == previous
    assert store.get_identities() == accounts


def test_display_label_edits_preserve_ids_history_and_reopening(store, tmp_path):
    rt = _configured(store, tmp_path)
    accounts = store.get_identities()
    for account in accounts:
        store.add_history_snapshot(account.id, 1, ["song|artist"])
    rt.apply_setup([{"id": account.id, "label": f"Renamed {account.label}",
        "credential_ref": account.credential_ref, "brand_account_id": account.brand_account_id,
        "is_master": account.is_master} for account in accounts])
    assert {i.id for i in store.get_identities()} == {i.id for i in accounts}
    reopened = Runtime(store, rt.config_path, tmp_path)
    reopened.load()
    assert reopened.configured
    assert set(reopened.clients()) == {i.id for i in accounts}
    assert {r[0] for r in store.conn.execute("SELECT identity_id FROM history_snapshots")} == {i.id for i in accounts}


def test_clients_use_bridge_wired_after_startup_load(store, tmp_path):
    from yt_playlist.core.bridge import Bridge
    rt = Runtime(store, tmp_path / "config.toml", tmp_path)
    rt.load()
    rt.bridge = Bridge()
    assert all(c._session._bridge is rt.bridge for c in rt.clients().values())


def test_setup_form_carries_stable_identity_on_rename(store, tmp_path):
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app
    rt = _configured(store, tmp_path)
    main, brand = store.get_identities()
    app = create_app(store, rt.clients, setup=rt, background_work=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/setup", headers={"accept": "application/json"}, data={
            "identity_id": [str(main.id), str(brand.id)], "label": ["New main", "New brand"],
            "brand_account_id": ["", brand.brand_account_id], "master": "0"})
        assert response.status_code == 200
    assert [(i.id, i.label) for i in store.get_identities()] == [(main.id, "New main"), (brand.id, "New brand")]
