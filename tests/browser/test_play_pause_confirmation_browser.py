"""Real card, HTTP, WebSocket and MV3 delivery confirm the actual media state."""
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment
from playwright.sync_api import expect
import pytest

from tests.browser import test_extension_lifecycle_browser as lifecycle
from tests.browser.test_extension_lifecycle_browser import installed_bridge  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src/yt_playlist/web/static"


@pytest.fixture
def playback_bridge(monkeypatch, request, playwright):
    if getattr(request, "param", None) == "strict":
        launch = playwright.chromium.launch_persistent_context

        def restricted_launch(profile, **kwargs):
            kwargs["args"] = [arg for arg in kwargs["args"] if not arg.startswith("--autoplay-policy=")]
            kwargs["args"] += ["--autoplay-policy=document-user-activation-required",
                               "--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies"]
            return launch(profile, **kwargs)

        monkeypatch.setattr(playwright.chromium, "launch_persistent_context", restricted_launch)
    h = request.getfixturevalue("installed_bridge")
    h.eventually(lambda: h.bridge.playback_confirmation)
    return h


def _card(h, width=1280):
    base = h.worker.evaluate("TC_CONFIG.appOrigin")
    fragment = Environment().from_string((ROOT /
        "src/yt_playlist/web/templates/_partials/now_playing.html").read_text()).render()
    html = '''<!doctype html><head><style>[x-cloak]{display:none!important}</style>
        <link rel="stylesheet" href="/static/tokens.css">
        <link rel="stylesheet" href="/static/components.css">
        <link rel="stylesheet" href="/static/playback.css">
        <script src="/static/playback-status.js"></script>
        <script src="/static/playback-controls.js"></script>
        <script src="/static/app.js"></script>
        <script src="/static/player-card.js"></script>
        <script defer src="/static/vendor/alpine.min.js"></script></head>
        <body><div id="toasts" class="toasts"></div><div class="sidebar-dock-slot"></div>
        <div class="global-now-playing-slot">''' + fragment + "</div></body>"
    h.context.route(base + "/controls", lambda route: route.fulfill(content_type="text/html", body=html))

    def asset(route):
        path = STATIC / urlsplit(route.request.url).path.removeprefix("/static/")
        route.fulfill(body=path.read_bytes(), content_type=mimetypes.guess_type(path)[0] or "text/plain")

    h.context.route(base + "/static/**", asset)
    app = h.context.new_page()
    app.set_viewport_size({"width": width, "height": 900})
    app.goto(base + "/controls")
    expect(app.locator(".global-now-playing")).to_be_visible()
    return app


def test_card_confirms_pause_resume_and_does_not_replay_stale_actions(playback_bridge):
    h = playback_bridge
    music = lifecycle._music(h, "?playing=true&v=confirmed")
    h.eventually(lambda: h.bridge.now_playing is not None)
    app = _card(h, 1280)
    player = app.locator(".global-now-playing")
    for action, paused in (("Pause", True), ("Play", False)):
        with app.expect_response("**/now-playing/toggle?*") as response:
            player.get_by_role("button", name=action, exact=True).click()
        assert response.value.json() == {"ok": True, "paused": paused}
        assert music.evaluate("document.querySelector('video').paused") is paused
        expect(player.get_by_role("button", name="Play" if paused else "Pause", exact=True)).to_be_enabled()
    assert music.evaluate("fixture.clicks") == ["playpause", "playpause"]
    # Explicit Play is idempotent, even if a stale UI sends it after another control resumed.
    result = app.request.post(h.worker.evaluate("TC_CONFIG.appOrigin") +
        "/now-playing/toggle?action=play&videoId=confirmed&confirm=true").json()
    assert result == {"ok": True, "paused": False}
    assert music.evaluate("fixture.clicks") == ["playpause", "playpause"]
    assert app.locator("#playback-error").count() == 0




@pytest.mark.parametrize("playback_bridge", ["strict"], indirect=True)
def test_blocked_autoplay_is_reported_and_music_activation_recovers(playback_bridge):
    h = playback_bridge
    music = h.context.new_page()
    music.goto("https://music.youtube.com/?v=activation")
    cdp = h.context.new_cdp_session(music)

    def inspect(expression):
        # Ordinary Playwright evaluation grants activation and would hide this failure.
        result = cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                              "awaitPromise": True, "userGesture": False})
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    h.eventually(lambda: inspect("!!(window.fixture && window.__tcNowPoller)"))
    h.eventually(lambda: h.bridge.now_playing is not None)
    app = _card(h)
    player = app.locator(".global-now-playing")
    assert inspect("navigator.userActivation.hasBeenActive") is False
    with app.expect_response("**/now-playing/toggle?*") as rejected:
        player.get_by_role("button", name="Play", exact=True).click()
    assert rejected.value.json()["ok"] is False
    expect(app.locator("#playback-error, [role=alert]:visible, .toast-err")).to_have_count(0)
    assert inspect("document.querySelector('video').paused") is True
    assert inspect("navigator.userActivation.hasBeenActive") is False
    expect(player.get_by_role("button", name="Play", exact=True)).to_be_enabled()
    music.get_by_role("button", name="Play or pause", exact=True).click()
    music.wait_for_function("!document.querySelector('video').paused")
    lifecycle._sample(h, music)
    app.evaluate("window.playbackStatus.refresh()")
    player.get_by_role("button", name="Pause", exact=True).click()
    expect(player.get_by_role("button", name="Play", exact=True)).to_be_enabled()
    player.get_by_role("button", name="Play", exact=True).click()
    expect(player.get_by_role("button", name="Pause", exact=True)).to_be_enabled()
    expect(app.locator("#playback-error")).to_have_count(0)
