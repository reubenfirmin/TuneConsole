"""Account setup, validation and reauthentication messages."""
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
from unittest.mock import Mock
from tests.home_fixture_support import without_metadata_providers
from tests.store_fixture_support import bounded_consumer_schema, uncontended_file_consumer_store as store

import pytest
from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.setup_support import _FakeRuntime


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture(autouse=True)
def setup_routes_only(monkeypatch):
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home, setup, taste

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (app_update, audio_preview, home, setup, taste)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)


def test_home_format_setting_is_in_setup_and_persists(
        store, unlocked_home_controls, without_metadata_providers, monkeypatch):
    from yt_playlist.rec import account_growth

    # Availability is a completed input here; growth tests own preparation and
    # unlock conditions. Exercise the real setting handlers and rendered forms.
    growth = account_growth.view(store, 1000)
    growth['ready_formats'] = dict.fromkeys(('mixes', 'albums', 'radio'), True)
    monkeypatch.setattr(account_growth, 'view', lambda *_args, **_kwargs: growth)
    with TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000.0),
                    base_url='http://127.0.0.1') as c:
        assert all(c.get('/home/growth').json()['ready_formats'].values())
        html = c.get('/setup?tab=home').text
        assert 'class="setup-home-formats"' in html
        assert 'Follow my listening' not in html
        assert 'hx-post="/home/format"' in html
        assert 'Start Home with' not in c.get('/taste/panels/recommendations').text
        assert c.post('/home/format', data={'format': 'radio'}).status_code == 204
        assert 'name="format" value="radio" checked' in c.get('/setup?tab=home').text
        assert c.post('/home/format', data={'format': 'invalid'}).status_code == 400
        assert store.settings.get_setting('home_format') == 'radio'


def test_unconfigured_home_still_renders(store):
    # The forced /setup redirect is gone by design: a default identity is auto-provisioned and the
    # extension pairs live, so the dashboard is reachable before setup. /setup stays available.
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200                          # home renders, no redirect
    assert c.get("/setup").status_code == 200            # setup page itself is reachable

def test_setup_post_applies_and_redirects(store):
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup", data={
        "label": ["main", "brand"],
        "brand_account_id": ["", "UC9"],
        "master": "0",
    }, follow_redirects=False)
    assert r.status_code == 303
    location = urlsplit(r.headers["location"])
    assert location.path == "/setup" and parse_qs(location.query)["tab"] == ["identities"]
    identities = rt.applied
    assert [i["label"] for i in identities] == ["main", "brand"]
    assert identities[0]["is_master"] and not identities[1]["is_master"]
    assert identities[1]["brand_account_id"] == "UC9"

def _setup_redirect(store, rt):
    """POST a valid single-identity setup and return its redirect query."""
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup", data={"label": "main", "brand_account_id": "", "master": "0"},
               follow_redirects=False)
    assert r.status_code == 303
    location = urlsplit(r.headers["location"])
    assert location.path == "/setup"
    return parse_qs(location.query)

def test_setup_first_time_flashes_saved(store):
    query = _setup_redirect(store, _FakeRuntime(store, configured=False))
    assert query["tab"] == ["identities"]
    assert query["flash"] == ["Saved 1 identity."]

def test_setup_existing_library_does_not_claim_reauthentication(store):
    store.settings.set_setting("last_sync_at", "1.0")
    query = _setup_redirect(store, _FakeRuntime(store, configured=True))
    assert query["tab"] == ["identities"]
    assert "toast" not in query
    assert "authenticated" not in query["flash"][0]

def test_setup_post_skips_blank_label_rows(store):
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.post("/setup", data={
        "headers": "h",
        "label": ["main", ""],            # second row blank -> dropped
        "brand_account_id": ["", ""],
        "master": "0",
    }, follow_redirects=False)
    identities = rt.applied
    assert [i["label"] for i in identities] == ["main"]

def test_setup_post_validation_error_rerenders_400(store):
    rt = _FakeRuntime(store, configured=False)
    rt.raise_value_error = "exactly one identity must be the master, found 0"
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/setup", data={"headers": "h", "label": "main", "brand_account_id": ""})
    assert r.status_code == 400
    assert "must be the master" in r.text

def test_setup_post_without_collaborator_404(store):
    # No setup collaborator -> wizard POST is inert (existing apps keep working).
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.post("/setup", data={"label": "x"}).status_code == 404

# --- network transparency page ---
def test_setup_page_alpine_attr_well_formed(store):
    # Regression: tojson emits double quotes, so x-data MUST be single-quoted or the JSON
    # terminates the HTML attribute early and Alpine (the whole check UI) never initializes.
    rt = _FakeRuntime(store, configured=False)
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    c = TestClient(app, base_url="http://127.0.0.1")
    body = c.get("/setup").text
    assert "x-data='setupForm(" in body          # single-quoted wrapper
    assert 'x-data="setupForm([{"' not in body   # not the broken double-quoted form


# --- streaming sync (background job + SSE) ---

def test_saving_identity_definitions_does_not_clear_expired_session():
    from fastapi import FastAPI
    from yt_playlist.controllers.accounts import Accounts
    from yt_playlist.repos.accounts import AccountRepo
    from yt_playlist.web.context import Ctx
    from yt_playlist.web.jobs import SyncJobs
    from yt_playlist.web.routes import setup

    iid = 1
    repository = Mock(spec_set=AccountRepo)
    repository.has_auth_state.return_value = True
    repository.auth_expirations.return_value = {}
    repository.flag_auth_expiration.return_value = {iid: 'Main'}
    repository.get_accounts.return_value = [SimpleNamespace(id=iid)]
    store = SimpleNamespace(accounts=Accounts(
        repository, None, paused=lambda: False, maintenance_accounts=lambda: None))
    rt = _FakeRuntime(store, configured=True)
    ctx = Ctx(store, rt.clients, lambda: 1.0, Mock(), SyncJobs(), setup=rt)
    ctx.flag_auth_expired(iid, 'Main')
    app = FastAPI()
    app.include_router(setup.build(ctx))
    with TestClient(app) as c:
        r = c.post("/setup", data={"label": ["main"],
                                   "brand_account_id": [""], "master": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/setup?tab=identities&flash=")
    assert ctx.auth_expired == {iid: "Main"}
    assert rt.applied[0]['label'] == 'main'
    repository.flag_auth_expiration.assert_called_once_with(iid, 'Main')
    repository.clear_auth_expirations.assert_not_called()


