import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.core.bridge import BridgeError
from yt_playlist.core.runtime import Runtime
from yt_playlist.web.app import create_app
from yt_playlist.web.context import AUTH_EXPIRED_KEY


class CheckedClient(FakeClient):
    def __init__(self, name, *, error=None, library_error=None, handle=None):
        super().__init__()
        self.name, self.handle, self.error, self.library_error = name, handle, error, library_error
        self.checks = 0

    def get_account_info(self):
        self.checks += 1
        if self.error:
            raise self.error
        return {"accountName": self.name, "channelHandle": self.handle}

    def get_library_playlists(self, limit=25):
        if self.library_error:
            raise self.library_error
        return super().get_library_playlists(limit)


@pytest.fixture
def identity_setup(store, tmp_path, monkeypatch):
    runtime = Runtime(store, tmp_path / "config.toml", tmp_path)
    clients = {"main": CheckedClient("Main account"),
               "Brand": CheckedClient("My brand", handle="@mybrand")}
    monkeypatch.setattr("yt_playlist.core.runtime.build_client", lambda cfg, bridge: clients[cfg.label])
    runtime.apply_setup([{"label": "main", "credential_ref": "browser.json", "is_master": True}])
    app = create_app(store, runtime.clients, now_fn=lambda: 1.0, setup=runtime)
    return SimpleNamespace(store=store, runtime=runtime, clients=clients, app=app,
                           client=TestClient(app, base_url="http://127.0.0.1"))


def _add_brand(t, brand="123456789012345678901"):
    return t.client.post("/setup", data={"label": ["main", "Brand"],
                         "brand_account_id": ["", brand], "master": "0"}, follow_redirects=False)


@pytest.mark.parametrize("has_synced", [False, True])
def test_add_identity_returns_to_identities_and_checks_actual_access(identity_setup, has_synced):
    t = identity_setup
    if has_synced:
        t.store.settings.set_setting("last_sync_at", "1.0")
    saved = _add_brand(t)
    assert saved.status_code == 303
    location = urlsplit(saved.headers["location"])
    assert location.path == "/setup"
    assert parse_qs(location.query)["tab"] == ["identities"]
    assert "authenticated" not in saved.headers["location"]
    ids = {i.label: i.id for i in t.store.identities.get_identities()}
    assert t.client.get(saved.headers["location"]).context["rows"] == [
        {"id": ids["main"], "label": "main", "brand": ""},
        {"id": ids["Brand"], "label": "Brand", "brand": "123456789012345678901"}]
    checked = t.client.get("/setup/identities/check")
    assert checked.status_code == 200
    assert {r["state"] for r in checked.json()["checks"]} == {"valid"}
    assert {r["heading"] for r in checked.json()["checks"]} == {"Authenticated"}
    assert t.clients["Brand"].checks == t.clients["main"].checks == 1
    # A valid identity can have no playlists yet; a check never imports or prunes library data.
    assert t.store.playlists.get_playlists() == []


def test_denied_brand_has_help_and_does_not_hide_successful_main(identity_setup):
    t = identity_setup
    _add_brand(t)
    t.clients["Brand"].error = RuntimeError("Server returned HTTP 403: Forbidden")
    checked = t.client.get("/setup/identities/check")
    states = {r["label"]: r["state"] for r in checked.json()["checks"]}
    assert states == {"main": "valid", "Brand": "invalid"}
    assert "owns or manages this brand account" in checked.text
    assert "https://myaccount.google.com/brandaccounts" in checked.text
    assert "save and check again" not in checked.text


@pytest.mark.parametrize("brand", ["https://example.com/b/123/", "@mybrand", "UC12345"])
def test_common_brand_id_mistakes_are_explained(identity_setup, brand):
    t = identity_setup
    _add_brand(t, brand)
    checked = t.client.get("/setup/identities/check")
    assert "Check the Brand account ID" in checked.text
    assert "rather than a full URL, channel ID, or @handle" in checked.text
    assert t.clients["Brand"].checks == 0


def test_disconnected_extension_is_unverified_and_does_not_clear_expired_state(identity_setup):
    t = identity_setup
    iid = t.store.identities.get_identities()[0].id
    t.app.state.ctx.flag_auth_expired(iid, "main")
    t.clients["main"].error = BridgeError("no extension connected")
    checked = t.client.get("/setup/identities/check")
    assert checked.json()["checks"][0]["state"] == "unverified"
    assert "signed-in music.youtube.com tab open" in checked.text
    assert checked.json()["checks"][0]["help_url"] is None
    assert "Open Pairing" not in checked.text
    assert "automatically" in checked.text
    assert t.app.state.ctx.auth_expired == {iid: "main"}
    assert json.loads(t.store.settings.get_setting(AUTH_EXPIRED_KEY)) == {str(iid): "main"}


def test_only_live_success_clears_its_expired_flag(identity_setup):
    t = identity_setup
    _add_brand(t)
    ids = {i.label: i.id for i in t.store.identities.get_identities()}
    for label, iid in ids.items():
        t.app.state.ctx.flag_auth_expired(iid, label)
    t.clients["Brand"].library_error = RuntimeError("Server returned HTTP 403: Forbidden")
    checked = t.client.get("/setup/identities/check")
    assert t.app.state.ctx.auth_expired == {ids["Brand"]: "Brand"}
    assert checked.json()["auth_labels"] == ["Brand"]
    t.clients["Brand"].library_error = None
    checked = t.client.get("/setup/identities/check")
    assert t.app.state.ctx.auth_expired == {}
    assert t.store.settings.get_setting(AUTH_EXPIRED_KEY) is None
    assert checked.json()["auth_labels"] == []


def test_expired_main_session_has_signin_guidance(identity_setup):
    t = identity_setup
    t.clients["main"].error = RuntimeError("Server returned HTTP 401: Unauthorized")
    checked = t.client.get("/setup/identities/check")
    assert "Sign in required" in checked.text
    assert "refresh the YouTube Music tab" in checked.text
    assert checked.json()["checks"][0]["help_url"] == "https://music.youtube.com"


def test_unexpected_response_is_not_reported_as_valid(identity_setup):
    t = identity_setup
    t.clients["main"].name = ""
    checked = t.client.get("/setup/identities/check")
    assert checked.json()["checks"][0]["state"] == "unverified"
    assert "YouTube returned an unexpected response" in checked.text


def test_blank_label_with_brand_is_not_silently_discarded(identity_setup):
    t = identity_setup
    response = t.client.post("/setup", data={"label": ["main", ""],
                             "brand_account_id": ["", "123456"], "master": "0"})
    assert response.status_code == 400
    assert "every identity needs a label" in response.text
    assert response.context["rows"] == [{"id": None, "label": "main", "brand": ""},
                                         {"id": None, "label": "", "brand": "123456"}]
    assert len(t.store.identities.get_identities()) == 1


def test_recheck_targets_only_the_selected_identity(identity_setup):
    t = identity_setup
    _add_brand(t)
    brand = next(i for i in t.store.identities.get_identities() if i.label == "Brand")
    checked = t.client.get("/setup/identities/check", params={"identity_id": brand.id})
    assert [r["identity_id"] for r in checked.json()["checks"]] == [brand.id]
    assert t.clients["Brand"].checks == 1 and t.clients["main"].checks == 0


def test_autosave_returns_saved_rows_without_redirect_or_auth_claim(identity_setup):
    t = identity_setup
    response = t.client.post("/setup", headers={"Accept": "application/json"}, data={
        "label": ["main", "Brand"], "brand_account_id": ["", "123456"], "master": "0"})
    assert response.status_code == 200
    assert "location" not in response.headers
    assert [i["label"] for i in response.json()["identities"]] == ["main", "Brand"]
    assert all(i["id"] for i in response.json()["identities"])
    assert response.json()["auth_labels"] == []
    assert t.clients["Brand"].checks == t.clients["main"].checks == 0
    t.runtime.load()
    assert [i.label for i in t.store.identities.get_identities()] == ["main", "Brand"]


def test_invalid_autosave_returns_error_and_preserves_saved_identities(identity_setup):
    t = identity_setup
    response = t.client.post("/setup", headers={"Accept": "application/json"}, data={
        "label": ["main", "main"], "brand_account_id": ["", "123456"], "master": "0"})
    assert response.status_code == 400
    assert response.json() == {"error": "identity labels must be unique"}
    assert [i.label for i in t.store.identities.get_identities()] == ["main"]


def test_remove_persists_without_checks_or_unrelated_form_changes(identity_setup):
    t = identity_setup
    _add_brand(t)
    ids = {i.label: i.id for i in t.store.identities.get_identities()}
    for label, iid in ids.items():
        t.app.state.ctx.flag_auth_expired(iid, label)
    snapshot = t.store.history.add_history_snapshot(ids["Brand"], 1.0, ["song|artist"])
    response = t.client.post(f'/setup/identities/{ids["Brand"]}/remove', data={"label": "unsaved edit"})
    assert response.status_code == 200
    assert response.json() == {"message": 'Removed identity "Brand".', "main_identity_id": ids["main"],
                               "identity_count": 1, "auth_labels": ["main"]}
    assert [i.label for i in t.store.identities.get_identities()] == ["main"]
    assert t.app.state.ctx.auth_expired == {ids["main"]: "main"}
    assert t.clients["Brand"].checks == t.clients["main"].checks == 0
    assert t.store.conn.execute("SELECT identity_id FROM history_snapshots WHERE id=?",
                                (snapshot,)).fetchone()[0] == ids["Brand"]
    t.runtime.load()
    assert set(t.runtime.clients()) == {ids["main"]}


def test_remove_main_promotes_a_remaining_identity(identity_setup):
    t = identity_setup
    _add_brand(t)
    ids = {i.label: i.id for i in t.store.identities.get_identities()}
    response = t.client.post(f'/setup/identities/{ids["main"]}/remove')
    assert response.status_code == 200
    assert response.json()["main_identity_id"] == ids["Brand"]
    assert [(i.label, i.is_master) for i in t.store.identities.get_identities()] == [("Brand", True)]
    t.runtime.load()
    assert t.store.identities.get_master_identity().id == ids["Brand"]


def test_remove_unknown_or_only_identity_leaves_config_unchanged(identity_setup):
    t = identity_setup
    original = t.runtime.config_path.read_text()
    only = t.store.identities.get_identities()[0]
    assert t.client.post("/setup/identities/999999/remove").status_code == 404
    assert t.client.post(f"/setup/identities/{only.id}/remove").status_code == 409
    assert t.runtime.config_path.read_text() == original
    assert t.store.identities.get_identities() == [only]


@pytest.mark.parametrize("brand_accessible", [True, False])
def test_live_checks_use_each_identity_through_the_browser_bridge(store, tmp_path, brand_accessible):
    calls = []
    brand_id = "123456789012345678901"

    class AccountBridge:
        def execute(self, method, url, body):
            endpoint = urlsplit(url).path.rsplit("/v1/", 1)[-1]
            user = body["context"].get("user", {}).get("onBehalfOfUser")
            calls.append((method, endpoint, user))
            if user == brand_id and not brand_accessible:
                return 403, json.dumps({"error": {"message": "Forbidden"}})
            if endpoint == "account/account_menu":
                header = {"accountName": {"runs": [{"text": "My brand" if user else "Main account"}]},
                          "accountPhoto": {"thumbnails": [{"url": "https://example.com/avatar.jpg"}]}}
                data = {"actions": [{"openPopupAction": {"popup": {"multiPageMenuRenderer": {
                    "header": {"activeAccountHeaderRenderer": header}}}}}]}
            else:
                assert endpoint == "browse" and body["browseId"] == "FEmusic_liked_playlists"
                data = {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
                    "content": {"sectionListRenderer": {"contents": [{"gridRenderer": {"items": []}}]}}}}]}}}
            return 200, json.dumps(data)

    runtime = Runtime(store, tmp_path / "config.toml", tmp_path)
    runtime.bridge = AccountBridge()
    runtime.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "is_master": True},
        {"label": "Brand", "credential_ref": "browser.json", "brand_account_id": brand_id},
    ])
    app = create_app(store, runtime.clients, now_fn=lambda: 1.0, setup=runtime)
    checked = TestClient(app, base_url="http://127.0.0.1").get("/setup/identities/check")
    states = {r["label"]: r["state"] for r in checked.json()["checks"]}
    assert states == {"main": "valid", "Brand": "valid" if brand_accessible else "invalid"}
    assert calls[:3] == [("POST", "account/account_menu", None), ("POST", "browse", None),
                         ("POST", "account/account_menu", brand_id)]
    if brand_accessible:
        assert calls[3:] == [("POST", "browse", brand_id)]
        assert checked.json()["checks"][1]["heading"] == "Authenticated"
    else:
        assert "Cannot access this brand account" in checked.text


def test_removing_identity_updates_setup_menu_and_move_choices(store, tmp_path):
    runtime = Runtime(store, tmp_path / "config.toml", tmp_path)
    runtime.apply_setup([
        {"label": "main", "credential_ref": "browser.json", "is_master": True},
        {"label": "Second identity", "credential_ref": "browser.json", "brand_account_id": "UC9"},
        {"label": "Third identity", "credential_ref": "browser.json", "brand_account_id": "UC10"},
    ])
    ids = {i.label: i.id for i in store.identities.get_identities()}
    store.playlists.upsert_playlist(ids["Second identity"], "PL2", "Removed identity mix", 0, "h", 1.0)
    app = create_app(store, runtime.clients, now_fn=lambda: 1.0, setup=runtime)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert '<a href="/move"' in client.get("/setup").text

    # Removing one of three accounts must also refresh the remaining Move dropdown choices.
    response = client.post(f'/setup/identities/{ids["Second identity"]}/remove')
    assert response.status_code == 200
    move = client.get("/move")
    assert '<a href="/move"' in move.text
    assert "Second identity" not in move.text
    assert "Removed identity mix" not in move.text
    assert {i.label for i in move.context["identities"]} == {"main", "Third identity"}

    # Removing the last secondary account hides Move and leaves no selectors on a direct visit.
    response = client.post(f'/setup/identities/{ids["Third identity"]}/remove')
    assert response.status_code == 200
    for path in ("/setup", "/playlists", "/move"):
        assert '<a href="/move"' not in client.get(path).text
    setup = client.get("/setup")
    assert setup.context["rows"] == [{"id": ids["main"], "label": "main", "brand": ""}]
    move = client.get("/move")
    assert "You need at least two identities" in move.text
    assert '<option value=' not in move.text
