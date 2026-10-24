from playwright.sync_api import expect

from yt_playlist.core import updatecheck
from tests.onboarding_support import publish_personal_mix


def update_card(monkeypatch, page, store, *, kind="flatpak"):
    store.settings.set_setting("last_sync_at", "1")
    publish_personal_mix(store, now=1)
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
    page.goto(f"{live_app}/#notices")
    card = page.locator("#update-nudge")
    expect(card.get_by_role("button", name="Update now", exact=True)).to_be_visible()
    card.get_by_role("button", name="Update now", exact=True).click()
    expect(card.get_by_role("progressbar")).to_have_attribute("value", "40")
    expect(card.get_by_role("button", name="Dismiss", exact=True)).to_be_disabled()
    assert calls["install"] == 1
    state.update(phase="ready", progress=100, message="Update installed. Restart when you're ready.")
    restart = card.get_by_role("button", name="Restart to update", exact=True)
    expect(restart).to_be_visible(timeout=5000)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
        restart.click()
        expect(card.get_by_role("status")).to_contain_text("Waiting for your sync")
    assert calls["restart"] == 1
    assert len(page.context.pages) == 1
    assert not errors


def test_lost_restart_reply_and_navigation_rejoin_pending_server_state(live_app, page, store, monkeypatch):
    state, calls = update_card(monkeypatch, page, store)
    state.update(phase="ready")

    def accepted_but_lost(route):
        calls["restart"] += 1
        state.update(phase="draining", can_cancel=True, message="Waiting for your sync to finish…")
        route.abort()

    page.route("**/app-update/restart", accepted_but_lost)
    # Keep the server pending until this test explicitly completes its work.
    page.route("**/app-update/status", lambda route: route.fulfill(json=dict(state)))
    page.goto(f"{live_app}/#notices")
    card = page.locator("#update-nudge")
    card.get_by_role("button", name="Restart to update", exact=True).click()
    expect(card.get_by_role("status")).to_contain_text("Waiting for your sync")
    page.goto(f"{live_app}/playlists")
    page.goto(f"{live_app}/#notices")
    expect(card.get_by_role("button", name="Cancel restart")).to_be_visible()
    expect(card.get_by_role("button", name="Dismiss", exact=True)).to_be_disabled()
    assert calls["restart"] == 1
    with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
        state.update(instance="new-process", phase="current", can_cancel=False)
    assert calls["restart"] == 1


def test_pending_restart_can_be_cancelled_from_the_card(live_app, page, store, monkeypatch):
    state, calls = update_card(monkeypatch, page, store)
    state.update(phase="draining", can_cancel=True, message="Waiting for background work to finish…")
    page.route("**/app-update/status", lambda route: route.fulfill(json=dict(state)))

    def cancel(route):
        state.update(phase="ready", can_cancel=False, message="Restart cancelled.")
        route.fulfill(json=dict(state))

    page.route("**/app-update/restart/cancel", cancel)
    page.goto(f"{live_app}/#notices")
    card = page.locator("#update-nudge")
    card.get_by_role("button", name="Cancel restart").click()
    expect(card.get_by_role("button", name="Restart to update", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="Cancel restart")).to_be_hidden()
    assert calls["restart"] == 0


def test_denial_opens_software_manager_fallback(live_app, page, store, monkeypatch):
    state, _ = update_card(monkeypatch, page, store)
    page.goto(f"{live_app}/#notices")
    state.update(phase="unavailable", message="Updates weren't allowed. Update in your software manager.")
    link = page.locator("#update-nudge").get_by_role("link", name="Open software manager")
    expect(link).to_be_visible(timeout=5000)
    expect(link).to_have_attribute("href", "appstream://com.tuneconsole.TuneConsole")
    expect(page.locator("#update-nudge code")).to_have_count(0)


def test_failed_download_can_retry(live_app, page, store, monkeypatch):
    state, calls = update_card(monkeypatch, page, store)
    page.goto(f"{live_app}/#notices")
    card = page.locator("#update-nudge")
    card.get_by_role("button", name="Update now", exact=True).click()
    expect(card.get_by_role("progressbar")).to_be_visible()
    state.update(phase="error", progress=None, message="The update couldn't be completed. Please try again.")
    retry = card.get_by_role("button", name="Try again", exact=True)
    expect(retry).to_be_visible(timeout=5000)
    retry.click()
    expect(card.get_by_role("progressbar")).to_be_visible()
    assert calls["install"] == 2


def test_macos_native_install_reconnects_after_listener_disappears(live_app, page, store, monkeypatch):
    state, calls = update_card(monkeypatch, page, store, kind="macos")
    page.goto(f"{live_app}/#notices")
    card = page.locator("#update-nudge")
    card.get_by_role("button", name="Update now", exact=True).click()
    expect(card.get_by_role("button", name="Dismiss", exact=True)).to_be_disabled()
    expect(card.get_by_role("progressbar")).to_have_count(0)  # Sparkle owns the progress window
    expect(card.get_by_role("button", name="Restart to update")).to_have_count(0)
    # Native Install and Relaunch can stop the listener between status polls. Detect
    # the new process without needing a separate restart POST or an old-process reply.
    page.route("**/app-update/status", lambda route: route.abort())
    expect(card.get_by_role("status")).to_contain_text("Couldn’t check", timeout=5000)
    page.unroute("**/app-update/status")
    with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
        state.update(instance="updated-mac-process", phase="current", message="Up to date.")
    assert calls == {"install": 1, "restart": 0}
    assert len(page.context.pages) == 1


def test_manual_relaunch_preserves_card_and_reconnects_to_new_instance(live_app, page, store, monkeypatch):
    state, calls = update_card(monkeypatch, page, store)
    state.update(phase="ready")

    def manual_restart(route):
        calls["restart"] += 1
        state.update(phase="manual_restart", message="Update installed. Close TuneConsole, then open it again.")
        route.fulfill(json=dict(state))

    page.route("**/app-update/restart", manual_restart)
    page.goto(f"{live_app}/#notices")
    card = page.locator("#update-nudge")
    card.get_by_role("button", name="Restart to update", exact=True).click()
    expect(card.get_by_role("status")).to_contain_text("Close TuneConsole")
    expect(card.get_by_role("button", name="Restart to update", exact=True)).to_be_hidden()
    expect(card.get_by_role("button", name="Update now", exact=True)).to_be_hidden()
    with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
        state.update(instance="fresh-launch", phase="current", message="Up to date.")
    assert calls == {"install": 0, "restart": 1}
    assert page.url == f"{live_app}/#notices"
    assert len(page.context.pages) == 1
