from playwright.sync_api import expect

from yt_playlist.core import updatecheck


def update_card(monkeypatch, page, store, *, kind="flatpak"):
    store.settings.set_setting("last_sync_at", "1")
    monkeypatch.setattr(updatecheck, "update_nudge", lambda _: {
        "kind": kind, "current": "0.1.9", "latest": "0.2.0",
        "command": "Update now", "link": None,
    })
    state = {"instance": "old-process", "phase": "available", "message": "", "progress": None}
    calls = {"install": 0, "restart": 0}

    def respond(route):
        operation = route.request.url.rsplit("/", 1)[-1]
        if operation == "install":
            calls["install"] += 1
            state.update(phase="installing", message="Downloading and installing the update…", progress=40)
        elif operation == "restart":
            calls["restart"] += 1
            if calls["restart"] == 1:
                route.fulfill(status=409, json={**state, "busy": True, "message": "Waiting for your sync to finish…"})
                return
            route.fulfill(status=202, json={**state, "phase": "restarting"})
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
    assert calls["restart"] == 2
    assert len(page.context.pages) == 1
    assert not errors


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
