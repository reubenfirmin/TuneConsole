from playwright.sync_api import expect

from tests.home_fixture_support import returning_listener
from tests.onboarding_support import publish_personal_mix
from yt_playlist.core import updatecheck


def update_card(monkeypatch, page, store, *, kind="flatpak"):
    store.settings.set_setting("last_sync_at", "1")
    publish_personal_mix(store, now=1)
    returning_listener(store, 'mixes', now=1)
    monkeypatch.setattr(updatecheck, "update_nudge", lambda _: {
        "kind": kind, "current": "0.1.9", "latest": "0.2.0",
        "command": "Update now", "link": None,
    })
    state = {"instance": "old-process", "phase": "available", "message": "", "progress": None}
    calls = {"install": 0, "restart": 0}
    restart_polls = 0

    def respond(route):
        nonlocal restart_polls
        operation = route.request.url.rsplit("/", 1)[-1]
        if operation == "install":
            calls["install"] += 1
            state.update(phase="installing", message="Downloading and installing the update…", progress=40)
        elif operation == "restart":
            calls["restart"] += 1
            state.update(phase="draining", busy=True, can_cancel=True, message="Waiting for your sync to finish…")
            route.fulfill(status=202, json=dict(state))
            return
        elif operation == "status" and state["phase"] == "draining":
            restart_polls += 1
            if restart_polls < 2:
                route.fulfill(json=dict(state))
                return
            route.fulfill(json={**state, "phase": "restarting", "can_cancel": False})
            state.update(instance="new-process", phase="current", message="Up to date.")
            return
        route.fulfill(json=dict(state))

    page.route("**/app-update/*", respond)
    return state, calls


def test_download_progress_then_restart_reconnects_same_tab(live_app, page, store, monkeypatch):
    state, calls = update_card(monkeypatch, page, store)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.clock.install()
    page.goto(f"{live_app}/#notices", wait_until='domcontentloaded')
    card = page.locator("#update-nudge")
    expect(card.get_by_role("button", name="Update now", exact=True)).to_be_visible()
    card.get_by_role("button", name="Update now", exact=True).click()
    expect(card.get_by_role("progressbar")).to_have_attribute("value", "40")
    expect(card.get_by_role("button", name="Dismiss", exact=True)).to_be_disabled()
    assert calls["install"] == 1
    state.update(phase="ready", progress=100, message="Update installed. Restart when you're ready.")
    page.clock.fast_forward(2100)
    restart = card.get_by_role("button", name="Restart to update", exact=True)
    expect(restart).to_be_visible(timeout=5000)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
        restart.click()
        expect(card.get_by_role("status")).to_contain_text("Waiting for your sync")
        page.clock.fast_forward(2100)
        page.wait_for_function('Alpine.$data(document.querySelector("#update-nudge")).phase === "restarting"')
        page.clock.fast_forward(1100)
    assert calls["restart"] == 1
    assert len(page.context.pages) == 1
    assert not errors
