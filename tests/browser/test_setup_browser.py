"""Live-behavior test for the Setup pairing tab: the token renders verbatim and the connection
indicator starts in the "waiting" state, polling GET /bridge/status via Alpine (see setup.html's
setupForm().init())."""
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.core.bridge import BridgeError
from yt_playlist.core.runtime import Runtime
from yt_playlist.web.app import create_app
from tests.setup_support import _FakeRuntime
from tests.web.test_setup_identities import CheckedClient

pytestmark = pytest.mark.browser


def test_home_starting_mode_saves_in_setup(live_app, store, page):
    page.goto(f'{live_app}/setup?tab=home')
    expect(page.get_by_role('tab', name='Home', exact=True)).to_have_attribute('aria-selected', 'true')
    choice = page.get_by_label('Start Home with', exact=True)
    expect(choice).to_be_visible()
    with page.expect_response('**/home/format') as response:
        choice.select_option('albums')
    assert response.value.status == 204
    expect(page.get_by_role('status').filter(has_text='Saved.')).to_be_visible()
    assert store.settings.get_setting('home_format') == 'albums'
    page.reload()
    expect(choice).to_have_value('albums')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_setup_app():
    s = Store(":memory:")
    s.init_schema()
    rt = _FakeRuntime(s, configured=False)
    app = create_app(s, rt.clients, now_fn=lambda: 1.0, setup=rt)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_setup_pairing_tab_shows_autoconnect_and_waiting_status(live_setup_app, page):
    page.goto(f"{live_setup_app}/setup")
    # Pairing is seamless now: no token to paste, just an auto-connecting extension.
    expect(page.get_by_text("it connects automatically")).to_be_visible()
    # No extension connected yet: GET /bridge/status polls back {"connected": false}
    expect(page.get_by_text("Extension not connected")).to_be_visible()


def test_setup_tab_deep_link_opens_enrichment(live_setup_app, page):
    # #69: the home Last.fm card lands on the Enrichment tab directly, not on Pairing.
    page.goto(f"{live_setup_app}/setup?tab=enrichment")
    expect(page.get_by_role("heading", name="Metadata providers")).to_be_visible()
    expect(page.get_by_text("Pair the browser extension", exact=True)).to_be_hidden()
    # Last.fm signup guidance: the callback URL is optional and the shared secret is irrelevant.
    expect(page.get_by_text("Callback URL")).to_be_visible()
    expect(page.get_by_text("Shared secret")).to_be_visible()


@pytest.mark.parametrize('width', [1440, 390])
def test_album_description_settings_save_pause_and_remove_without_exposing_key(live_setup_app, page, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(f'{live_setup_app}/setup?tab=enrichment')
    panel = page.get_by_role('region', name='DeepSeek', exact=True)
    key = panel.get_by_label('Connect DeepSeek', exact=True)
    expect(key).to_have_attribute('type', 'text')
    key.fill('sk-test-0123456789')
    expect(key).to_have_value('sk-test-0123456789')
    panel.get_by_role('button', name='Save key', exact=True).click()
    expect(panel.get_by_text('API key saved', exact=False)).to_be_visible()
    expect(key).to_have_value('')
    expect(panel.get_by_role('button', name='Remove key', exact=True)).to_be_visible()
    panel.get_by_role('button', name='Replace', exact=True).click()
    expect(key).to_be_visible()
    key.fill('sk-replacement-0123456789')
    panel.get_by_role('button', name='Save key', exact=True).click()
    expect(key).to_be_hidden()
    panel.get_by_label('Maximum requests per day', exact=True).fill('8')
    panel.get_by_label('Maximum requests per day', exact=True).blur()
    expect(panel.get_by_role('status')).to_have_text('Album description settings saved.')
    expect(panel.get_by_label('Connect DeepSeek', exact=True)).to_have_value('')
    assert 'sk-test-0123456789' not in page.content()
    assert 'sk-replacement-0123456789' not in page.content()
    with page.expect_response('**/settings/deepseek') as saved:
        panel.locator('.provider-toggle').click()
    assert saved.value.ok
    expect(panel.get_by_role('switch', name='Enable DeepSeek', exact=True)).not_to_be_checked()
    expect(panel.get_by_role('status')).to_have_text('Album description settings saved.')
    page.reload()
    panel = page.get_by_role('region', name='DeepSeek', exact=True)
    expect(panel.get_by_role('switch', name='Enable DeepSeek', exact=True)).not_to_be_checked()
    expect(panel.get_by_label('Maximum requests per day', exact=True)).to_have_value('8')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    # The whole provider list must align, not just the isolated new control.
    page.screenshot(path=f'/tmp/tuneconsole-deepseek-settings-{width}.png', full_page=True)
    lastfm = page.locator('.provider-card[data-name="lastfm"]')
    for selector in ('.provider-icon', '.provider-heading', '.provider-toggle', '.provider-key'):
        assert abs(lastfm.locator(selector).bounding_box()['x'] - panel.locator(selector).bounding_box()['x']) < 2
    expect(panel.get_by_role('button', name='Save', exact=True)).to_have_count(0)
    assert panel.get_by_label('Maximum requests per day').evaluate(
        "el => getComputedStyle(el).backgroundColor") == page.locator('#lastfm-api-key').evaluate(
        "el => getComputedStyle(el).backgroundColor")
    panel.get_by_role('button', name='Remove key', exact=True).click()
    expect(panel.get_by_role('status')).to_contain_text('saved key removed')
    expect(panel.get_by_role('button', name='Remove key', exact=True)).to_be_hidden()


def test_setup_identities_tab_accepts_manual_label(live_setup_app, page):
    page.goto(f"{live_setup_app}/setup")
    page.get_by_role("tab", name="Identities").click()
    page.locator('input[name="label"]').first.fill("Reuben")
    expect(page.locator('input[name="label"]').first).to_have_value("Reuben")


@pytest.fixture
def live_identity_setup(tmp_path, monkeypatch):
    store = Store(":memory:")
    store.init_schema()
    store.settings.set_setting("last_sync_at", "1.0")  # previously made any identity edit claim reauthentication
    runtime = Runtime(store, tmp_path / "config.toml", tmp_path)
    clients = {"main": CheckedClient("Main account"), "Brand": CheckedClient("My brand", handle="@mybrand")}
    monkeypatch.setattr("yt_playlist.core.runtime.build_client", lambda cfg, bridge: clients[cfg.label])
    runtime.apply_setup([{"label": "main", "credential_ref": "browser.json", "is_master": True}])
    app = create_app(store, runtime.clients, now_fn=lambda: 1.0, setup=runtime)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "test server did not start"
        time.sleep(0.02)
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{port}", clients=clients, store=store, ctx=app.state.ctx)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        store.conn.close()


def _add_identity(page, base):
    page.goto(f"{base}/setup?tab=identities")
    page.get_by_role("button", name="+ Add identity", exact=True).click()
    page.get_by_label("Label for identity 2", exact=True).fill("Brand")
    page.get_by_label("Brand account ID for identity 2", exact=True).fill("123456789012345678901")
    page.get_by_label("Brand account ID for identity 2", exact=True).blur()
    expect(page.locator('form[x-ref="form"]')).to_have_attribute("aria-busy", "false")
    page.get_by_role("button", name="Close Brand account ID help", exact=True).click()


@pytest.mark.parametrize("viewport", [{"width": 1920, "height": 1080}, {"width": 1280, "height": 900}])
def test_identity_guide_stays_open_while_typing_and_saving(live_identity_setup, page, viewport):
    page.set_viewport_size(viewport)
    page.goto(f"{live_identity_setup.base}/setup?tab=identities")
    page.get_by_role("button", name="+ Add identity", exact=True).click()
    guide = page.get_by_role("region", name="Find your Brand account ID", exact=True)
    expect(guide).to_be_visible()

    label = page.get_by_label("Label for identity 2", exact=True)
    brand = page.get_by_label("Brand account ID for identity 2", exact=True)
    label.click()
    label.press_sequentially("Brand")
    expect(guide).to_be_visible()
    brand.click()
    brand.press_sequentially("123456789012345678901")
    expect(guide).to_be_visible()
    label.click()
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    expect(guide).to_be_visible()
    expect(label).to_have_value("Brand")
    expect(brand).to_have_value("123456789012345678901")

    page.get_by_role("button", name="Close Brand account ID help", exact=True).click()
    expect(guide).to_be_hidden()
    page.get_by_role("button", name="How to find your Brand account ID", exact=True).click()
    expect(guide).to_be_visible()
    page.keyboard.press("Escape")
    expect(guide).to_be_hidden()


def test_add_identity_stays_on_identities_and_shows_live_validation(live_identity_setup, page):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page).to_have_url(f"{t.base}/setup?tab=identities")
    expect(page.get_by_role("tab", name="Identities", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("status")).to_be_hidden()
    expect(page.get_by_label("Label for identity 2", exact=True)).to_have_value("Brand")
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    expect(page.get_by_role("button", name="Brand: authenticated", exact=True)).to_be_visible()
    expect(page.get_by_role("region", name="Identity connection checks")).to_have_count(0)
    expect(page.get_by_text("My brand", exact=False)).to_have_count(0)
    expect(page.get_by_role("button", name="Save and check", exact=True)).to_be_hidden()
    expect(page.get_by_text("You’re authenticated again", exact=False)).to_have_count(0)
    page.reload()
    expect(page.get_by_role("tab", name="Identities", exact=True)).to_have_attribute("aria-selected", "true")


def test_invalid_identity_stays_editable_and_explains_how_to_fix_it(live_identity_setup, page):
    t = live_identity_setup
    t.clients["Brand"].error = RuntimeError("Server returned HTTP 403: Forbidden")
    _add_identity(page, t.base)
    fields = page.locator('form[x-ref="form"] input:not([type="hidden"])')
    positions = fields.evaluate_all("fields => fields.map(field => { const r = field.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })")
    page.get_by_role("button", name="Brand: Cannot access this brand account. Show connection help.", exact=True).click()
    assert fields.evaluate_all("fields => fields.map(field => { const r = field.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })") == positions
    expect(page.get_by_text("Cannot access this brand account", exact=True)).to_be_visible()
    expect(page.get_by_text("owns or manages this brand account", exact=False)).to_be_visible()
    help_panel = page.get_by_role("region", name="Connection help for Brand", exact=True)
    expect(help_panel.get_by_role("link", name="Open this identity’s account details", exact=True)).to_have_attribute(
        "href", "https://myaccount.google.com/b/123456789012345678901/")
    expect(help_panel.get_by_role("link", name="Open YouTube Music", exact=True)).to_be_hidden()
    expect(help_panel.get_by_role("link", name="Google Brand Accounts", exact=True)).to_have_attribute("href", "https://myaccount.google.com/brandaccounts")
    expect(help_panel).to_contain_text("using the same Google account signed in to YouTube Music")
    expect(help_panel).to_contain_text("Copy the number after /b/")
    expect(help_panel).to_contain_text("compare it with the Brand account ID above")
    expect(help_panel).to_contain_text("For a personal account, leave the field blank")
    expect(help_panel.get_by_role("button")).to_have_count(0)
    expect(page.get_by_role("link", name="Open Pairing", exact=True)).to_have_count(0)
    expect(page.get_by_label("Brand account ID for identity 2", exact=True)).to_have_value("123456789012345678901")
    # Editing hides the previous result; saving cannot retarget the stored account.
    page.get_by_label("Brand account ID for identity 2", exact=True).fill("99999")
    expect(page.get_by_text("Cannot access this brand account", exact=True)).to_be_hidden()
    expect(page.get_by_role("button", name="Brand: authentication updates after editing", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Save and check", exact=True)).to_have_count(0)
    page.get_by_label("Brand account ID for identity 2", exact=True).blur()
    expect(page.get_by_role("alert")).to_contain_text(
        "An existing account cannot be retargeted; add the other account separately")
    assert next(i for i in t.store.identities.get_identities() if i.label == "Brand").brand_account_id == "123456789012345678901"


def test_disconnected_identity_is_checked_automatically_after_reconnecting(live_identity_setup, page):
    t = live_identity_setup
    connection = {"connected": False}
    page.route("**/bridge/status", lambda route: route.fulfill(json=connection))
    t.clients["Brand"].error = BridgeError("no extension connected")
    _add_identity(page, t.base)
    page.get_by_role("button", name="Brand: Not checked. Show connection help.", exact=True).click()
    expect(page.get_by_text("Not checked", exact=True)).to_be_visible()
    expect(page.get_by_text("TuneConsole extension installed", exact=False)).to_be_visible()
    expect(page.get_by_role("link", name="Open Pairing", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Check again", exact=True)).to_have_count(0)
    t.clients["Brand"].error = None
    connection["connected"] = True
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    expect(page.locator(".identity-auth-help:visible")).to_have_count(0)


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}])
def test_yellow_dot_help_keeps_fields_in_place_and_can_be_dismissed(live_identity_setup, page, viewport):
    page.set_viewport_size(viewport)
    t = live_identity_setup
    t.clients["Brand"].error = BridgeError("no extension connected")
    _add_identity(page, t.base)
    dot = page.get_by_role("button", name="Brand: Not checked. Show connection help.", exact=True)
    expect(dot).to_be_visible()
    fields = page.locator('form[x-ref="form"] input:not([type="hidden"])')
    positions = fields.evaluate_all("fields => fields.map(field => { const r = field.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })")
    dot.click()
    panel = page.get_by_role("region", name="Connection help for Brand", exact=True)
    expect(panel).to_be_visible()
    assert fields.evaluate_all("fields => fields.map(field => { const r = field.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })") == positions
    expect(dot).to_have_attribute("aria-expanded", "true")
    expect(panel.get_by_role("button")).to_have_count(0)
    dot.click()
    expect(panel).to_be_hidden()
    dot.click()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    dot.click()
    expect(panel).to_be_visible()


def test_successful_check_clears_expired_banner_without_reload(live_identity_setup, page):
    t = live_identity_setup
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    identity = t.store.identities.get_identities()[0]
    t.ctx.flag_auth_expired(identity.id, identity.label)
    t.clients["main"].error = RuntimeError("Server returned HTTP 401: Unauthorized")
    page.goto(f"{t.base}/setup?tab=identities")
    page.get_by_role("button", name="main: Sign in required. Show connection help.", exact=True).click()
    expect(page.get_by_text("Sign in required", exact=True)).to_be_visible()
    expect(page.locator(".authbar")).to_be_visible()
    expect(page.get_by_role("link", name="Open YouTube Music", exact=True)).to_be_visible()
    t.clients["main"].error = None
    # The browser sends focus when the user returns after signing in to YouTube Music.
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.locator(".identity-auth--valid")).to_have_count(1)
    expect(page.locator(".authbar")).to_be_hidden()
    assert not errors


def test_returning_from_brand_account_details_checks_automatically(live_identity_setup, page):
    t = live_identity_setup
    t.clients["Brand"].error = RuntimeError("Server returned HTTP 403: Forbidden")
    _add_identity(page, t.base)
    page.get_by_role("button", name="Brand: Cannot access this brand account. Show connection help.", exact=True).click()
    # Keep the external page local to the test while exercising the link and return behavior.
    page.context.route("https://myaccount.google.com/brandaccounts", lambda route: route.fulfill(
        content_type="text/html", body="<title>Brand Accounts</title>"))
    with page.expect_popup() as opened:
        page.get_by_role("region", name="Connection help for Brand", exact=True).get_by_role(
            "link", name="Google Brand Accounts", exact=True).click()
    details = opened.value
    details.wait_for_load_state()
    t.clients["Brand"].error = None
    details.close()
    page.bring_to_front()
    # Headless tabs retain visibility; send the event a normal browser emits on returning.
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    expect(page.locator(".identity-auth-help")).to_be_hidden()


def test_remove_is_immediate_with_deletion_message_and_no_checks(live_identity_setup, page):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    checks_before = {name: client.checks for name, client in t.clients.items()}
    page.get_by_role("button", name="Remove Brand", exact=True).click()
    expect(page.locator('input[name="label"]')).to_have_count(1)
    expect(page.get_by_role("status")).to_contain_text('Removed identity "Brand".')
    expect(page.get_by_role("button", name="Save and check", exact=True)).to_be_hidden()
    expect(page.locator('.tools-pop a[href="/move"]')).to_have_count(0)
    assert [i.label for i in t.store.identities.get_identities()] == ["main"]
    assert {name: client.checks for name, client in t.clients.items()} == checks_before
    page.reload()
    expect(page.get_by_role("tab", name="Identities", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator('input[name="label"]')).to_have_count(1)
    expect(page.get_by_role("status")).to_contain_text('Removed identity "Brand".')
    expect(page.get_by_text("Saved 1 identity.", exact=False)).to_have_count(0)


def test_remove_finishes_pending_edits_without_an_extra_click(live_identity_setup, page):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    t.clients["Still editing"] = CheckedClient("Still editing")
    page.get_by_label("Label for identity 1", exact=True).fill("Still editing")
    page.get_by_role("button", name="Remove Brand", exact=True).click()
    expect(page.locator('input[name="label"]')).to_have_count(1)
    expect(page.get_by_label("Label for identity 1", exact=True)).to_have_value("Still editing")
    expect(page.get_by_role("button", name="Save and check", exact=True)).to_have_count(0)
    assert [i.label for i in t.store.identities.get_identities()] == ["Still editing"]


def test_failed_removal_keeps_the_row_and_can_be_retried(live_identity_setup, page):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    page.route("**/setup/identities/*/remove", lambda route: route.fulfill(
        status=409, content_type="application/json", body='{"detail":"Could not remove this identity. Try again."}'))
    page.get_by_role("button", name="Remove Brand", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("Could not remove this identity. Try again.")
    expect(page.locator('input[name="label"]')).to_have_count(2)
    assert len(t.store.identities.get_identities()) == 2
    page.unroute("**/setup/identities/*/remove")
    page.get_by_role("button", name="Remove Brand", exact=True).click()
    expect(page.locator('input[name="label"]')).to_have_count(1)


def test_discarding_an_unsaved_row_needs_no_save(live_identity_setup, page):
    t = live_identity_setup
    page.goto(f"{t.base}/setup?tab=identities")
    page.get_by_role("button", name="+ Add identity", exact=True).click()
    page.get_by_role("button", name="Remove identity 2", exact=True).click()
    expect(page.locator('input[name="label"]')).to_have_count(1)
    expect(page.get_by_role("button", name="Save and check", exact=True)).to_be_hidden()
    assert [i.label for i in t.store.identities.get_identities()] == ["main"]


def test_main_choice_saves_immediately_without_auth_checks(live_identity_setup, page):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    checks_before = {name: client.checks for name, client in t.clients.items()}
    page.get_by_label("Use Brand as main identity", exact=True).check()
    expect(page.locator('form[x-ref="form"]')).to_have_attribute("aria-busy", "false")
    assert t.store.identities.get_master_identity().label == "Brand"
    assert {name: client.checks for name, client in t.clients.items()} == checks_before
    page.reload()
    expect(page.get_by_label("Use Brand as main identity", exact=True)).to_be_checked()


@pytest.mark.parametrize("first_fails", [False, True])
def test_changes_during_an_autosave_are_saved_in_order(live_identity_setup, page, first_fails):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    original = next(i for i in t.store.identities.get_identities() if i.label == "Brand")
    t.clients["First label"] = t.clients["Second label"] = t.clients["Brand"]
    held = []

    def hold_first(route):
        if not held:
            held.append(route)
        else:
            route.continue_()

    page.route("**/setup?tab=identities", hold_first)
    label = page.get_by_label("Label for identity 2", exact=True)
    label.fill("First label")
    label.blur()
    expect(page.locator('form[x-ref="form"]')).to_have_attribute("aria-busy", "true")
    assert len(held) == 1
    label.fill("Second label")
    label.blur()
    if first_fails:
        held[0].fulfill(status=503, content_type="application/json", body='{"error":"Temporary failure"}')
    else:
        held[0].fulfill(response=held[0].fetch())
    expect(page.locator('form[x-ref="form"]')).to_have_attribute("aria-busy", "false")
    saved = next(i for i in t.store.identities.get_identities() if i.label == "Second label")
    assert (saved.id, saved.brand_account_id) == (original.id, original.brand_account_id)
    expect(label).to_have_value("Second label")
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    expect(page.get_by_role("alert")).to_have_count(0)


def test_removing_main_selects_remaining_main_without_saving(live_identity_setup, page):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    page.get_by_role("button", name="Remove main", exact=True).click()
    expect(page.locator('input[name="label"]')).to_have_count(1)
    expect(page.get_by_label("Use Brand as main identity", exact=True)).to_be_checked()
    expect(page.get_by_role("button", name="Save and check", exact=True)).to_be_hidden()
    assert t.store.identities.get_master_identity().label == "Brand"


def test_form_error_keeps_identity_tab_and_all_entered_values(live_identity_setup, page):
    t = live_identity_setup
    page.goto(f"{t.base}/setup")
    page.get_by_role("tab", name="Identities", exact=True).click()
    page.get_by_role("button", name="+ Add identity", exact=True).click()
    page.get_by_label("Label for identity 2", exact=True).fill("main")
    page.get_by_label("Brand account ID for identity 2", exact=True).fill("123456789012345678901")
    page.get_by_label("Brand account ID for identity 2", exact=True).blur()
    expect(page.get_by_text("identity labels must be unique", exact=True)).to_be_visible()
    expect(page.get_by_role("tab", name="Identities", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.get_by_label("Label for identity 2", exact=True)).to_have_value("main")
    expect(page.get_by_label("Brand account ID for identity 2", exact=True)).to_have_value("123456789012345678901")
    page.get_by_label("Label for identity 2", exact=True).fill("Brand")
    page.get_by_label("Label for identity 2", exact=True).blur()
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
