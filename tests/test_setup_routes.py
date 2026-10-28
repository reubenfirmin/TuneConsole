"""Identity setup, validation and reauthentication messages."""
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.setup_support import _FakeRuntime
def test_home_format_setting_is_in_setup_and_persists(store):
    c = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1.0), base_url='http://127.0.0.1')
    html = c.get('/setup?tab=home').text
    assert 'id="setup-home-format"' in html
    assert 'hx-post="/home/format"' in html
    assert 'Start Home with' not in c.get('/taste/panels/recommendations').text
    assert c.post('/home/format', data={'format': 'radio'}).status_code == 204
    assert '<option value="radio" selected>' in c.get('/setup?tab=home').text
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

def test_saving_identity_definitions_does_not_clear_expired_session(store):
    rt = _FakeRuntime(store, configured=True)
    iid = store.identities.upsert_identity("main", "browser.json", None, True)
    store.settings.set_setting("last_sync_at", "1.0")
    app = create_app(store, rt.clients, now_fn=lambda: 1.0, setup=rt)
    app.state.ctx.flag_auth_expired(iid, "Main")
    c = TestClient(app, base_url="http://127.0.0.1")

    r = c.post("/setup", data={"label": ["main"],
                               "brand_account_id": [""], "master": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/setup?tab=identities&flash=")
    assert app.state.ctx.auth_expired == {iid: "Main"}


