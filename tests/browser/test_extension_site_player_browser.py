"""Public-site connection changes, setup instructions, and keyboard access."""
from pathlib import Path
import time

import pytest
from playwright.sync_api import expect


EXT = Path(__file__).resolve().parents[2] / "extension"
HEADER = """<nav class="site-nav" aria-label="Main navigation">
<a class="brand" href="#top">TuneConsole</a><div class="nav-links">
<a href="#features">Features</a><a class="nav-install" href="#install">Get TuneConsole</a>
</div></nav>"""


def _site(page, platform="Linux", *, header=True, pending=False):
    page.route("https://tuneconsole.com/**", lambda route: route.fulfill(
        content_type="text/html", body=HEADER if header else "<main>Loading</main>"))
    page.goto("https://tuneconsole.com/")
    page.evaluate("""({platform, pending}) => {
      Object.defineProperty(navigator, 'userAgentData', {value: {platform}, configurable: true});
      Object.defineProperty(navigator, 'maxTouchPoints', {value: 0, configurable: true});
      window.tcListeners = [];
      window.chrome = {runtime: {
        id: 'test-extension',
        onMessage: {addListener: fn => tcListeners.push(fn)},
        sendMessage: msg => {
          if (msg.type === 'open-local-player') {
            window.tcOpened = true;
            return Promise.resolve({opened: true, connected: true});
          }
          return pending ? new Promise(resolve => window.tcResolveStatus = resolve)
                         : Promise.resolve({connected: false});
        },
      }};
      window.tcStatus = connected => tcListeners.forEach(fn => fn({type: 'local-player-status', connected}));
    }""", {"platform": platform, "pending": pending})
    page.add_style_tag(path=str(EXT / "site_player.css"))
    page.add_script_tag(path=str(EXT / "site_player.js"))


@pytest.mark.parametrize(("platform", "instruction", "download"), [
    ("Linux", "flatpak run com.tuneconsole.TuneConsole", "Download the Linux Flatpak"),
    ("macOS", "Open the DMG and drag TuneConsole to Applications.", "Apple Silicon"),
    ("Windows", "docker start tuneconsole", None),
])
def test_offline_badge_opens_platform_instructions(page, platform, instruction, download):
    _site(page, platform)
    badge = page.get_by_role("link", name="Local Player, offline", exact=True)
    expect(badge).to_have_css("background-color", "rgb(229, 231, 235)")
    badge.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Your local player")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text(instruction, exact=False)).to_be_visible()
    if download:
        expect(dialog.get_by_role("link", name=download)).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(badge).to_be_focused()


def test_status_changes_update_open_dialog_and_player_destination(page):
    _site(page)
    badge = page.locator("#tuneconsole-local-player")
    badge.click()
    dialog = page.get_by_role("dialog")
    page.evaluate("tcStatus(true)")
    expect(badge).to_have_css("background-color", "rgb(21, 233, 140)")
    expect(dialog.get_by_text("Your local player is ready.")).to_be_visible()
    expect(dialog.get_by_text("flatpak run com.tuneconsole.TuneConsole")).not_to_be_visible()
    expect(dialog.get_by_role("link", name="Open local player")).to_be_visible()
    dialog.get_by_role("button", name="Close").click()
    page.evaluate("tcStatus(false)")
    badge.click()
    expect(dialog.get_by_text("flatpak run com.tuneconsole.TuneConsole")).to_be_visible()
    page.evaluate("tcStatus(true)")
    dialog.get_by_role("button", name="Close").click()
    badge.click()
    assert page.evaluate("tcOpened")
    expect(page).to_have_url("https://tuneconsole.com/")


@pytest.mark.parametrize("connect_before_mount", [False, True])
def test_setup_page_opens_the_app_when_already_connected(page, connect_before_mount):
    _site(page, header=False, pending=True)
    if connect_before_mount:
        page.evaluate("tcStatus(true); tcResolveStatus({connected: false})")
    page.locator("body").evaluate("""body => body.innerHTML = `
      <main data-tc-extension-setup>
        <h1>Extension installed!</h1><p>Install and start the app.</p>
      </main>`""")
    if not connect_before_mount:
        expect(page.get_by_role("heading", name="Extension installed!")).to_be_visible()
        expect(page).to_have_url("https://tuneconsole.com/")
        page.evaluate("tcStatus(true); tcResolveStatus({connected: false})")
    page.wait_for_function("window.tcOpened === true")
    expect(page).to_have_url("https://tuneconsole.com/")


def test_setup_changes_to_payoff_only_after_the_app_connects(page):
    _site(page, header=False, pending=True)
    page.locator("body").evaluate("""body => body.innerHTML = `
      <main data-tc-extension-setup>
        <div data-tc-setup-install><h1 tabindex="-1">Extension installed!</h1>
          <h2>Install and start the app.</h2></div>
        <section data-tc-setup-ready hidden>
          <h1 tabindex="-1">Time to rock out!</h1>
          <a data-tc-setup-open href="http://127.0.0.1:8765/">Open TuneConsole</a>
        </section>
      </main>`""")
    page.evaluate("tcResolveStatus({connected: false})")
    expect(page.get_by_role("heading", name="Install and start the app.")).to_be_visible()
    expect(page.get_by_role("heading", name="Time to rock out!")).not_to_be_visible()
    page.evaluate("tcStatus(true)")
    expect(page.get_by_role("heading", name="Install and start the app.")).not_to_be_visible()
    expect(page.get_by_role("heading", name="Time to rock out!")).to_be_focused()
    expect(page).to_have_url("https://tuneconsole.com/")
    page.get_by_role("link", name="Open TuneConsole", exact=True).click()
    assert page.evaluate("tcOpened")
    page.evaluate("tcStatus(false)")
    expect(page.get_by_role("heading", name="Install and start the app.")).to_be_visible()
    expect(page.get_by_role("heading", name="Time to rock out!")).not_to_be_visible()


def test_delayed_header_and_stale_status_reply_do_not_lose_connected_state(page):
    _site(page, header=False, pending=True)
    page.evaluate("tcStatus(true)")
    page.evaluate("tcResolveStatus({connected: false})")
    page.locator("body").evaluate("(body, header) => body.insertAdjacentHTML('afterbegin', header)", HEADER)
    badge = page.get_by_role("link", name="Local Player, running", exact=True)
    expect(badge).to_be_visible()
    page.add_script_tag(path=str(EXT / "site_player.js"))
    expect(page.locator("#tuneconsole-local-player")).to_have_count(1)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_real_extension_reuses_player_tab_in_another_window(playwright, browser_type_launch_args, tmp_path, host):
    """Use Chrome's actual tabs API: mocked URL queries missed alias/match-pattern bugs."""
    context = playwright.chromium.launch_persistent_context(
        tmp_path, **{**browser_type_launch_args, "channel": "chromium"},
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
    )
    try:
        context.route("https://tuneconsole.com/**", lambda route: route.fulfill(
            content_type="text/html", body=HEADER))
        context.route(f"http://{host}:8765/**", lambda route: route.fulfill(
            content_type="text/html", body="<h1>Local player</h1>"))
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
        # Let fresh-install navigation finish before arranging inactive player tabs.
        # Otherwise its initial connection wait can activate the player during this test.
        worker.evaluate("""async () => {
            const deadline = Date.now() + 5000;
            while (Date.now() < deadline) {
                const tabs = await chrome.tabs.query({});
                if (tabs.some(tab => [tab.url, tab.pendingUrl].some(url =>
                    url === 'https://tuneconsole.com/extension-setup' ||
                    url === 'http://127.0.0.1:8765/'))) return;
                await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw Error('Initial extension navigation did not finish');
        }""")
        # A real app may already be running on the developer's machine. Its fresh-install
        # tab belongs to this isolated browser profile; remove it before arranging one player.
        worker.evaluate(r"""async () => {
            const tabs = await chrome.tabs.query({});
            const initial = tabs.filter(tab => [tab.url, tab.pendingUrl].some(url =>
                /^http:\/\/(127\.0\.0\.1|localhost):8765\//.test(url || '')));
            if (initial.length) await chrome.tabs.remove(initial.map(tab => tab.id));
        }""")
        # Supply only the bridge's connected state; tab discovery, activation, window focus,
        # content scripts, and runtime messaging all use the installed extension and real APIs.
        player = context.new_page()
        player.goto(f"http://{host}:8765/playlists?sort=recent")
        context.new_page()  # Leave the player inactive within its own window.
        with context.expect_page() as new_page:
            worker.evaluate("chrome.windows.create({url: 'about:blank'})")
        site = new_page.value
        site.goto("https://tuneconsole.com/")
        worker.evaluate("""async () => {
            Object.defineProperty(tuneConsole.transport, 'connected', {get: () => true});
            await tuneConsole.site.publish();
        }""")
        badge = site.get_by_role("link", name="Local Player, running", exact=True)
        expect(badge).to_be_visible()
        before = worker.evaluate("chrome.tabs.query({})")
        player_tab = next(tab for tab in before if tab.get("url") == player.url)
        assert not player_tab["active"]
        badge.click()
        # Observe the real handler's result without invoking a second open request in the test.
        deadline = time.monotonic() + 5
        while True:
            after = worker.evaluate("chrome.tabs.query({})")
            active = next(tab for tab in after if tab["id"] == player_tab["id"])["active"]
            focused = browser_type_launch_args.get("headless", True) or (
                worker.evaluate("chrome.windows.getLastFocused()")["id"] == player_tab["windowId"])
            if (active and focused) or time.monotonic() >= deadline:
                break
            site.wait_for_timeout(25)
        assert {tab["id"] for tab in after} == {tab["id"] for tab in before}
        assert next(tab for tab in after if tab["id"] == player_tab["id"])["active"]
        # Headless Chromium reports every window focused; OS focus needs a headed run.
        if not browser_type_launch_args.get("headless", True):
            focused = worker.evaluate("chrome.windows.getLastFocused()")
            assert focused["id"] == player_tab["windowId"]
        assert player.url == f"http://{host}:8765/playlists?sort=recent"
        assert site.url == "https://tuneconsole.com/"
    finally:
        context.close()
