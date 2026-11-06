"""Extension-only setup remains dismissible and follows the real connection state."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest
from playwright.sync_api import expect


EXT = Path(__file__).resolve().parents[2] / "extension"
SETUP_URL = "https://tuneconsole.com/extension-setup"


def _inject(page, *, pending=False):
    page.evaluate("""pending => {
      window.tcListeners = [];
      window.chrome = {runtime: {
        id: 'test-extension',
        getURL: path => 'https://extension.example/' + path,
        onMessage: {
          addListener: fn => tcListeners.push(fn),
          removeListener: fn => { tcListeners = tcListeners.filter(item => item !== fn); },
        },
        sendMessage: () => pending ? new Promise(resolve => window.tcResolveStatus = resolve)
                                  : Promise.resolve({connected: false}),
      }};
      window.tcStatus = connected => tcListeners.forEach(fn => fn({type: 'music-setup-status', connected}));
    }""", pending)
    page.route("https://extension.example/icons/*", lambda route: route.fulfill(
        path=str(EXT / "icons" / route.request.url.rsplit("/", 1)[-1])))
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
    panel = reminder.locator("section")
    badge = reminder.get_by_role("button", name="TuneConsole app not connected. Open setup reminder")
    expect(reminder).to_have_count(0)
    page.clock.fast_forward(5000)
    expect(panel).to_be_visible()
    expect(reminder.get_by_role("link", name="Get the app")).to_have_attribute(
        "href", "https://tuneconsole.com/extension-setup")
    reminder.get_by_role("button", name="Dismiss setup reminder").click()
    expect(panel).to_be_hidden()
    expect(badge).to_be_focused()
    page.reload()
    _inject(page)
    page.clock.fast_forward(6000)
    expect(panel).to_be_hidden()
    expect(badge).to_be_visible()
    badge.focus()
    page.keyboard.press("Enter")
    expect(panel).to_be_visible()
    expect(panel.get_by_role("button", name="Dismiss setup reminder")).to_be_focused()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(badge).to_be_focused()
    assert page.evaluate("localStorage.getItem('tuneconsole-setup-shown')") == "1"
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
    expect(reminder.locator("section")).to_be_visible()
    page.add_script_tag(path=str(EXT / "music_setup.js"))
    expect(reminder).to_have_count(1)
    page.evaluate("tcStatus(true)")
    expect(reminder).to_have_count(0)
    page.evaluate("chrome.runtime.id = null; document.dispatchEvent(new Event('visibilitychange'))")
    assert page.evaluate("window.__tcMusicSetup.stopped")
    expect(reminder).to_have_count(0)


def test_storage_failure_uses_badge_without_repeated_prompts(page):
    _music(page, pending=True)
    page.evaluate("""() => {
      Storage.prototype.setItem = () => { throw new Error('Storage unavailable'); };
      tcResolveStatus({connected: false});
    }""")
    page.clock.fast_forward(5000)
    reminder = page.locator("#tuneconsole-setup")
    expect(reminder.locator("section")).to_be_hidden()
    reminder.get_by_role("button", name="TuneConsole app not connected. Open setup reminder").click()
    expect(reminder.locator("section")).to_be_visible()
    reminder.get_by_role("button", name="Dismiss setup reminder").click()
    page.evaluate("tcStatus(true)")
    expect(reminder).to_have_count(0)
    page.evaluate("tcStatus(false)")
    page.clock.fast_forward(5000)
    expect(reminder.locator("section")).to_be_hidden()


def test_connecting_while_waiting_for_prompt_lock_does_not_spend_a_display(page):
    _music(page, pending=True)
    page.evaluate("""() => new Promise(acquired => {
      navigator.locks.request('tuneconsole-setup-shown', () => {
        const held = new Promise(resolve => window.tcReleasePromptLock = resolve);
        acquired();
        return held;
      });
    })""")
    page.evaluate("tcResolveStatus({connected: false})")
    page.clock.fast_forward(5000)
    page.evaluate("tcStatus(true); tcReleasePromptLock()")
    expect(page.locator("#tuneconsole-setup")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('tuneconsole-setup-shown')") is None


def _wait_prompt_locks(page, *, pending, held):
    page.wait_for_function("""async ({pending, held}) => {
        const locks = await navigator.locks.query();
        const queued = locks.pending.filter(lock => lock.name === 'tuneconsole-setup-shown').length;
        const owned = locks.held.some(lock => lock.name === 'tuneconsole-setup-shown');
        return queued === pending && owned === held;
    }""", arg={'pending': pending, 'held': held}, timeout=5000)


@pytest.mark.parametrize('release_after_grace', [False, True])
def test_reconnect_cancels_old_prompt_and_preserves_new_disconnect_grace(page, release_after_grace):
    _music(page, pending=True)
    page.clock.pause_at(page.evaluate('Date.now() + 1000'))
    page.evaluate("""() => new Promise(acquired => {
      navigator.locks.request('tuneconsole-setup-shown', () => {
        const held = new Promise(resolve => window.tcReleasePromptLock = resolve);
        acquired();
        return held;
      });
    })""")
    page.evaluate('tcResolveStatus({connected: false})')
    page.clock.fast_forward(5000)
    _wait_prompt_locks(page, pending=1, held=True)
    page.evaluate('tcStatus(true); tcStatus(false)')
    _wait_prompt_locks(page, pending=0, held=True)
    reminder = page.locator('#tuneconsole-setup')
    expect(reminder).to_have_count(0)
    assert page.evaluate("localStorage.getItem('tuneconsole-setup-shown')") is None
    if not release_after_grace:
        page.evaluate('tcReleasePromptLock()')
        _wait_prompt_locks(page, pending=0, held=False)
    page.clock.fast_forward(4999)
    expect(reminder).to_have_count(0)
    assert page.evaluate("localStorage.getItem('tuneconsole-setup-shown')") is None
    # Repeated reports of the same disconnect must not restart its deadline.
    page.evaluate('tcStatus(false)')
    page.clock.fast_forward(1)
    if release_after_grace:
        _wait_prompt_locks(page, pending=1, held=True)
        page.evaluate('tcReleasePromptLock()')
    _wait_prompt_locks(page, pending=0, held=False)
    expect(reminder.locator('section')).to_be_visible()
    assert page.evaluate("localStorage.getItem('tuneconsole-setup-shown')") == '1'


def test_stopping_cancels_queued_automatic_prompt(page):
    _music(page, pending=True)
    page.clock.pause_at(page.evaluate('Date.now() + 1000'))
    page.evaluate("""() => new Promise(acquired => {
      navigator.locks.request('tuneconsole-setup-shown', () => {
        const held = new Promise(resolve => window.tcReleasePromptLock = resolve);
        acquired();
        return held;
      });
    })""")
    page.evaluate('tcResolveStatus({connected: false})')
    page.clock.fast_forward(5000)
    _wait_prompt_locks(page, pending=1, held=True)
    page.evaluate('window.__tcMusicSetup.stop()')
    _wait_prompt_locks(page, pending=0, held=True)
    page.evaluate('tcReleasePromptLock()')
    _wait_prompt_locks(page, pending=0, held=False)
    page.clock.fast_forward(10000)
    expect(page.locator('#tuneconsole-setup')).to_have_count(0)
    assert page.evaluate("localStorage.getItem('tuneconsole-setup-shown')") is None


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


def _disconnected_extension(tmp_path):
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
    return extension


@pytest.fixture
def setup_site(tmp_path):
    # Extension-created tabs can navigate before Playwright attaches routing.
    # Serve even that first request locally while retaining the exact public URL.
    key, certificate = tmp_path / "setup.key", tmp_path / "setup.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(certificate), "-days", "1",
                    "-subj", "/CN=tuneconsole.com"], check=True, capture_output=True, timeout=10)
    requests = []

    class SetupHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b"<h1>Extension setup</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), SetupHandler) as server:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certificate, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
        thread.start()
        try:
            yield server.server_port, requests
        finally:
            server.shutdown()
            thread.join()


def _wait_setup_tab(worker, context, observations, stage):
    started = time.monotonic()
    state = {"stage": stage, "polls": 0}
    observations.append(state)
    while True:
        tabs = worker.evaluate("chrome.tabs.query({})")
        state["polls"] += 1
        state.setdefault("first_tabs", tabs)
        state["last_tabs"] = tabs
        state["seconds"] = time.monotonic() - started
        setup = [tab for tab in tabs if SETUP_URL in (tab.get("url"), tab.get("pendingUrl"))]
        assert len(setup) <= 1, f"Duplicate setup destinations: {state}"
        if setup and setup[0].get("url") == SETUP_URL and setup[0].get("status") == "complete":
            return setup
        assert state["seconds"] < 5, f"Extension did not finish opening its setup destination: {state}"
        # Pump browser events while polling the actual Chrome tab state.
        # Worker availability precedes its asynchronous installation callback.
        context.pages[0].wait_for_timeout(25)


@pytest.mark.parametrize("action", ["reload", "enable"])
def test_real_extension_reopens_setup_after_development_reload_or_enable(
    playwright, browser_type_launch_args, tmp_path, action, setup_site,
):
    extension = _disconnected_extension(tmp_path)
    port, requests = setup_site
    context = playwright.chromium.launch_persistent_context(
        tmp_path / "profile", **{**browser_type_launch_args, "channel": "chromium"},
        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}",
              "--ignore-certificate-errors", "--no-proxy-server",
              f"--host-resolver-rules=MAP tuneconsole.com 127.0.0.1:{port}, MAP www.tuneconsole.com 127.0.0.1:{port}"],
    )
    observations, events = [], []

    def observe_worker(worker):
        worker.on("console", lambda message: events.append(
            {"event": "worker-console", "type": message.type, "text": message.text, "url": worker.url}))
        worker.on("close", lambda closed: events.append({"event": "worker-closed", "url": closed.url}))

    context.on("serviceworker", observe_worker)
    for worker in context.service_workers:
        observe_worker(worker)
    context.on("weberror", lambda error: events.append({"event": "page-error", "error": str(error.error)}))
    try:
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
        if action == "enable" and not worker.evaluate("!!chrome.runtime.onEnabled"):
            pytest.skip("The re-enable event requires Chrome 155 or newer")
        # Observe first-install readiness before opening the manager, whose own
        # navigation must not be an implicit delay for installation to finish.
        first = _wait_setup_tab(worker, context, observations, "first-install")
        assert len(first) == 1
        assert worker.evaluate("tuneConsole.transport.connected") is False
        worker.evaluate("ids => chrome.tabs.remove(ids)", [tab["id"] for tab in first])
        manager = context.new_page()
        manager.goto("chrome://extensions/")
        item = manager.locator("extensions-item").filter(has_text="TuneConsole")
        if action == "reload":
            manager.locator("#devMode").click()
            with context.expect_page() as opened:
                item.locator("#dev-reload-button").click()
        else:
            item.locator("#enableToggle").click()
            with context.expect_page() as opened:
                item.locator("#enableToggle").click()
        expect(opened.value).to_have_url(SETUP_URL)
        expect(opened.value.get_by_role("heading", name="Extension setup", exact=True)).to_be_visible()
        worker = context.service_workers[0]
        reopened = _wait_setup_tab(worker, context, observations, action)
        assert len(reopened) == 1 and reopened[0]["id"] != first[0]["id"]
        assert requests.count("/extension-setup") == 2
    finally:
        # Captured on failure, or retained for passing runs with junit_logging=all.
        print(json.dumps({"action": action, "setup_readiness": observations, "events": events,
                          "local_setup_requests": requests}))
        context.close()


def test_real_extension_shares_two_prompts_across_tabs_and_browser_restarts(
    playwright, browser_type_launch_args, tmp_path, setup_site,
):
    extension = _disconnected_extension(tmp_path)
    port, _ = setup_site

    def launch():
        context = playwright.chromium.launch_persistent_context(
            tmp_path / "profile", **{**browser_type_launch_args, "channel": "chromium"},
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}",
                  "--ignore-certificate-errors", "--no-proxy-server",
                  f"--host-resolver-rules=MAP tuneconsole.com 127.0.0.1:{port}, MAP www.tuneconsole.com 127.0.0.1:{port}"],
        )
        context.route("https://music.youtube.com/**", lambda route: route.fulfill(
            content_type="text/html", body="""<body style="background:#101010;color:white">
            <h1>YouTube Music</h1><button>Play</button></body>"""))
        return context

    context = launch()
    try:
        # All three real content scripts reach their grace deadline together.
        pages = [context.new_page() for _ in range(3)]
        for page in pages:
            page.goto("https://music.youtube.com/")
        for page in pages:
            expect(page.locator("#tuneconsole-setup")).to_be_visible(timeout=10000)
        expanded = [page for page in pages if page.locator("#tuneconsole-setup section").is_visible()]
        quiet = [page for page in pages if not page.locator("#tuneconsole-setup section").is_visible()]
        assert len(expanded) == 2
        assert len(quiet) == 1
        assert pages[0].evaluate("localStorage.getItem('tuneconsole-setup-shown')") == "2"
        badge = quiet[0].get_by_role("button", name="TuneConsole app not connected. Open setup reminder")
        expect(badge).to_be_visible()
        expect(badge.locator(".attention")).to_have_text("!")
        quiet[0].wait_for_function("""() => {
          const img = document.querySelector('#tuneconsole-setup').shadowRoot.querySelector('img');
          return img.complete && img.naturalWidth === 48;
        }""")
        quiet[0].screenshot(path=str(tmp_path / "setup-badge.png"))
        badge.click()
        expect(quiet[0].locator("#tuneconsole-setup section")).to_be_visible()
        quiet[0].screenshot(path=str(tmp_path / "setup-reminder.png"))
        assert pages[0].evaluate("localStorage.getItem('tuneconsole-setup-shown')") == "2"
    finally:
        context.close()

    context = launch()
    try:
        page = context.new_page()
        page.goto("https://music.youtube.com/")
        badge = page.get_by_role("button", name="TuneConsole app not connected. Open setup reminder")
        expect(badge).to_be_visible(timeout=10000)
        expect(page.locator("#tuneconsole-setup section")).to_be_hidden()
        assert page.evaluate("localStorage.getItem('tuneconsole-setup-shown')") == "2"
    finally:
        context.close()
