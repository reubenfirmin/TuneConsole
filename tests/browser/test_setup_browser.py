"""Identity validation and ordered autosave through the rendered Setup form."""
import socket
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.live_server import LiveServer
from tests.web.test_setup_identities import CheckedClient
from yt_playlist.core.runtime import Runtime
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@contextmanager
def _live_server(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning",
        ))
        with LiveServer(server, sockets=[sock], startup_timeout=5):
            yield f"http://127.0.0.1:{port}"


@pytest.fixture
def live_identity_setup(store, tmp_path, monkeypatch):
    store.settings.set_setting("last_sync_at", "1.0")
    runtime = Runtime(store, tmp_path / "config.toml", tmp_path)
    clients = {"main": CheckedClient("Main account"), "Brand": CheckedClient("My brand", handle="@mybrand")}
    monkeypatch.setattr("yt_playlist.core.runtime.build_client", lambda cfg, bridge: clients[cfg.label])
    runtime.apply_setup([{"label": "main", "credential_ref": "browser.json", "is_master": True}])
    app = create_app(store, runtime.clients, now_fn=lambda: 1.0, setup=runtime)
    with _live_server(app) as base:
        yield SimpleNamespace(base=base, clients=clients, store=store, ctx=app.state.ctx)


def _add_identity(page, base):
    page.goto(f"{base}/setup?tab=identities")
    page.get_by_role("button", name="+ Add identity", exact=True).click()
    page.get_by_label("Label for identity 2", exact=True).fill("Brand")
    page.get_by_label("Brand account ID for identity 2", exact=True).fill("123456789012345678901")
    page.get_by_label("Brand account ID for identity 2", exact=True).blur()
    expect(page.locator('form[x-ref="form"]')).to_have_attribute("aria-busy", "false")
    page.get_by_role("button", name="Close Brand account ID help", exact=True).click()


def test_add_identity_stays_on_identities_and_shows_live_validation(live_identity_setup, page):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page).to_have_url(f"{t.base}/setup?tab=identities")
    expect(page.get_by_role("tab", name="Identities", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator('[role="status"][x-show="identityMessage"]')).to_be_hidden()
    expect(page.get_by_label("Label for identity 2", exact=True)).to_have_value("Brand")
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    expect(page.get_by_role("button", name="Brand: authenticated", exact=True)).to_be_visible()
    expect(page.get_by_role("region", name="Account connection checks")).to_have_count(0)
    expect(page.get_by_text("My brand", exact=False)).to_have_count(0)
    expect(page.get_by_role("button", name="Save and check", exact=True)).to_be_hidden()
    expect(page.get_by_text("You’re authenticated again", exact=False)).to_have_count(0)
    page.reload()
    expect(page.get_by_role("tab", name="Identities", exact=True)).to_have_attribute("aria-selected", "true")


@pytest.mark.parametrize("first_fails", [True])
def test_changes_during_an_autosave_are_saved_in_order(live_identity_setup, page, first_fails):
    t = live_identity_setup
    _add_identity(page, t.base)
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    original = next(i for i in t.store.accounts.get_accounts() if i.label == "Brand")
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
    held[0].fulfill(status=503, content_type="application/json", body='{"error":"Temporary failure"}')
    expect(page.locator('form[x-ref="form"]')).to_have_attribute("aria-busy", "false")
    saved = next(i for i in t.store.accounts.get_accounts() if i.label == "Second label")
    assert (saved.id, saved.brand_account_id) == (original.id, original.brand_account_id)
    expect(label).to_have_value("Second label")
    expect(page.locator(".identity-auth--valid")).to_have_count(2)
    expect(page.get_by_role("alert")).to_have_count(0)
