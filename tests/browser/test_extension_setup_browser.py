"""Extension-only setup remains dismissible and follows the real connection state."""
from pathlib import Path
import json
import shutil
import socket

import pytest
from playwright.sync_api import expect


EXT = Path(__file__).resolve().parents[2] / "extension"


def _inject(page, *, pending=False):
    page.evaluate("""pending => {
      window.tcListeners = [];
      window.chrome = {runtime: {
        id: 'test-extension',
        onMessage: {
          addListener: fn => tcListeners.push(fn),
          removeListener: fn => { tcListeners = tcListeners.filter(item => item !== fn); },
        },
        sendMessage: () => pending ? new Promise(resolve => window.tcResolveStatus = resolve)
                                  : Promise.resolve({connected: false}),
      }};
      window.tcStatus = connected => tcListeners.forEach(fn => fn({type: 'music-setup-status', connected}));
    }""", pending)
    page.add_script_tag(path=str(EXT / "music_setup.js"))


def _music(page, *, pending=False):
    page.clock.install()
    page.route("https://music.youtube.com/**", lambda route: route.fulfill(
        content_type="text/html", body="<h1>YouTube Music</h1><button>Play</button>"))
    page.goto("https://music.youtube.com/")
    _inject(page, pending=pending)


def test_setup_reminder_waits_and_stays_dismissed_after_reload(page):
    _music(page)
    reminder = page.locator("#tuneconsole-setup")
    expect(reminder).to_have_count(0)
    page.clock.fast_forward(5000)
    expect(reminder).to_be_visible()
    expect(reminder.get_by_role("link", name="Get the app")).to_have_attribute(
        "href", "https://tuneconsole.com/extension-setup")
    reminder.get_by_role("button", name="Dismiss setup reminder").click()
    expect(reminder).to_have_count(0)
    page.reload()
    _inject(page)
    page.clock.fast_forward(6000)
    expect(reminder).to_have_count(0)
    expect(page.get_by_role("button", name="Play", exact=True)).to_be_visible()


def test_stale_reply_and_repeated_disconnects_preserve_the_grace_period(page):
    _music(page, pending=True)
    reminder = page.locator("#tuneconsole-setup")
    page.evaluate("tcStatus(true); tcResolveStatus({connected: false})")
    page.clock.fast_forward(6000)
    expect(reminder).to_have_count(0)
    page.evaluate("tcStatus(false)")
    page.clock.fast_forward(3000)
    page.evaluate("tcStatus(false)")  # Bridge reconnect attempts publish every three seconds.
    page.clock.fast_forward(2000)
    expect(reminder).to_be_visible()
    page.add_script_tag(path=str(EXT / "music_setup.js"))
    expect(reminder).to_have_count(1)
    page.evaluate("tcStatus(true)")
    expect(reminder).to_have_count(0)
    page.evaluate("chrome.runtime.id = null; document.dispatchEvent(new Event('visibilitychange'))")
    assert page.evaluate("window.__tcMusicSetup.stopped")


def test_toolbar_explains_the_app_requirement_and_updates_status(page):
    page.clock.install()
    page.route("https://extension.example/**", lambda route: route.fulfill(
        path=str(EXT / route.request.url.rsplit("/", 1)[-1])))
    page.add_init_script("""window.tcConnected = false;
      window.chrome = {runtime: {sendMessage: async () => ({connected: tcConnected})}};""")
    page.goto("https://extension.example/setup.html")
    expect(page.get_by_role("status")).to_have_text("App not connected")
    expect(page.get_by_text("Listening history is saved only while", exact=False)).to_be_visible()
    expect(page.get_by_role("link", name="Get the app")).to_be_visible()
    expect(page.get_by_role("link", name="Get the app")).to_have_attribute(
        "href", "https://tuneconsole.com/extension-setup")
    expect(page.get_by_role("link", name="Open TuneConsole")).to_be_hidden()
    page.evaluate("tcConnected = true")
    page.clock.fast_forward(3000)
    expect(page.get_by_role("status")).to_have_text("App connected")
    expect(page.get_by_role("link", name="Get the app")).to_be_hidden()
    expect(page.get_by_role("link", name="Open TuneConsole")).to_be_visible()


@pytest.mark.parametrize("action", ["reload", "enable"])
def test_real_extension_reopens_setup_after_development_reload_or_enable(
    playwright, browser_type_launch_args, tmp_path, action,
):
    # Keep the user's running app out of this deliberately disconnected scenario.
    extension = tmp_path / "extension"
    shutil.copytree(EXT, extension)
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    config = extension / "worker" / "config.js"
    config.write_text(config.read_text().replace(":8765", f":{port}"))
    manifest = extension / "manifest.json"
    data = json.loads(manifest.read_text())
    data["host_permissions"] = [url.replace(":8765", f":{port}") for url in data["host_permissions"]]
    manifest.write_text(json.dumps(data))
    context = playwright.chromium.launch_persistent_context(
        tmp_path / "profile", **{**browser_type_launch_args, "channel": "chromium"},
        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
    )
    try:
        context.route("https://tuneconsole.com/**", lambda route: route.fulfill(
            content_type="text/html", body="<h1>Extension setup</h1>"))
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
        if action == "enable" and not worker.evaluate("!!chrome.runtime.onEnabled"):
            pytest.skip("The re-enable event requires Chrome 155 or newer")
        manager = context.new_page()
        manager.goto("chrome://extensions/")
        # First install must have opened the dedicated page already.
        first = worker.evaluate("chrome.tabs.query({url: 'https://tuneconsole.com/extension-setup'})")
        assert len(first) == 1
        worker.evaluate("ids => chrome.tabs.remove(ids)", [tab["id"] for tab in first])
        item = manager.locator("extensions-item").filter(has_text="TuneConsole")
        if action == "reload":
            manager.locator("#devMode").click()
            with context.expect_page() as opened:
                item.locator("#dev-reload-button").click()
        else:
            item.locator("#enableToggle").click()
            with context.expect_page() as opened:
                item.locator("#enableToggle").click()
        expect(opened.value).to_have_url("https://tuneconsole.com/extension-setup")
    finally:
        context.close()
