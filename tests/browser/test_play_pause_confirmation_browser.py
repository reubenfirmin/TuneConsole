"""Real card, HTTP, WebSocket and MV3 delivery confirm the actual media state."""
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
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
    fragment = Environment(loader=FileSystemLoader(ROOT / "src/yt_playlist/web/templates")).get_template(
        "_partials/now_playing.html").render()
    html = '''<!doctype html><head><style>[x-cloak]{display:none!important}</style>
        <link rel="stylesheet" href="/static/tokens.css">
        <link rel="stylesheet" href="/static/components.css">
        <link rel="stylesheet" href="/static/playback.css">
        <link rel="stylesheet" href="/static/album-listening.css">
        <script src="/static/playback-status.js"></script>
        <script src="/static/playback-controls.js"></script>
        <script src="/static/app.js"></script>
        <script src="/static/player-card.js"></script>
        <script src="/static/album-listening.js"></script>
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
    music = lifecycle._music(h, "?playing=true&v=confirmed&audible=true")
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

@pytest.mark.parametrize("native_state", ["audible", "silent", "other-media", "recent-audio", "tab-muted",
                                            "stalled-clock", "stale-queue", "stale-media", "moved-occurrence"])
def test_card_resumes_the_selected_playlist_entry_after_backend_restart(playback_bridge, native_state):
    h = playback_bridge
    video_id = "playlist-tail"
    h.store.tracks.upsert_track(video_id, "First song", "Recording artist", None, 6)
    silent = native_state in {"silent", "other-media", "recent-audio"}
    music = lifecycle._music(h, f"?v={video_id}&audible={str(not silent).lower()}")
    music.wait_for_function("document.querySelector('video').readyState >= 2")
    music.evaluate("""async state => {
        const host = document.querySelector('ytmusic-player');
        const video = host.querySelector('video');
        // The provider media is distinct from an unrelated, earlier media element.
        document.body.prepend(document.createElement('video'));
        // A complete buffer makes the raw timeline seekable independently of HTTP ranges.
        video.src = URL.createObjectURL(await (await fetch(video.currentSrc)).blob());
        video.loop = false;
        host.playerApi.getCurrentTime = () => state === 'stalled-clock' ? 0 : Math.max(0, video.currentTime - 24);
        host.playerApi.getPlayerResponse = () => ({videoDetails: {
            videoId: state === 'stale-media' ? 'previous-upload' : fixture.videoId,
            lengthSeconds: '6',
        }});
        const queue = document.createElement('ytmusic-player-queue');
        const selected = {
            videoId: state === 'stale-queue' ? 'different-upload' : fixture.videoId,
            navigationEndpoint: {watchEndpoint: {playlistId: fixture.playlist, index: 7}},
        };
        fixture.setQueueIndex = index => { selected.navigationEndpoint.watchEndpoint.index = index; };
        queue.queue = {store: {store: {getState: () => ({queue: {
            selectedItemIndex: 0, items: [{playlistPanelVideoRenderer: selected}],
        }})}}};
        document.body.appendChild(queue);
    }""", native_state)
    media = music.locator("ytmusic-player video")
    music.wait_for_function("document.querySelector('ytmusic-player video').readyState >= 2", timeout=5000)
    media.evaluate("video => {video.currentTime = 24}")
    music.wait_for_function("""() => {
        const video = document.querySelector('ytmusic-player video');
        return !video.seeking && Math.abs(video.currentTime - 24) < .002;
    }""", timeout=5000)
    lifecycle._sample(h, music)
    h.eventually(lambda: (h.bridge.resumable_player or {}).get("video_id") == video_id)
    original_bridge = h.bridge
    h.restart_backend()
    assert h.bridge is not original_bridge
    h.eventually(lambda: (h.bridge.resumable_player or {}).get("position") == 0)
    assert h.bridge.resumable_player["paused"] is True
    def tab_audible():
        return h.worker.evaluate(
            """async url => (await chrome.tabs.query({})).find(tab => tab.url === url).audible""", music.url)

    if native_state in {"other-media", "recent-audio"}:
        music.evaluate("""async state => {
            const audio = document.body.appendChild(document.createElement('audio'));
            audio.src = '/fixture.wav?audible=true';
            audio.loop = true;
            await audio.play();
            if (state === 'recent-audio') {
                document.querySelector('#play-pause-button').addEventListener('click', () => audio.pause(),
                    {capture: true, once: true});
            }
        }""", native_state)
        h.eventually(tab_audible)
    elif native_state == "tab-muted":
        h.worker.evaluate("""async url => {
            const tab = (await chrome.tabs.query({})).find(tab => tab.url === url);
            await chrome.tabs.update(tab.id, {muted: true});
        }""", music.url)
    app = _card(h)
    player = app.locator(".global-now-playing")
    expect(player.get_by_text("First song", exact=True)).to_be_visible()
    status_url = h.worker.evaluate("TC_CONFIG.appOrigin") + "/bridge/status"
    before = app.request.get(status_url).json()["now_playing"]
    assert (before["video_id"], before["position"], before["duration"], before["paused"]) == (video_id, 0, 6, True)
    if native_state == "moved-occurrence":
        app.wait_for_function("window.playbackStatus.snapshot?.now_playing?.queue_item?.index === 7")
        # The extension can report a new occurrence before the displayed card's next poll.
        app.evaluate("window.playbackStatus.stop()")
        music.evaluate("fixture.setQueueIndex(8)")
        lifecycle._sample(h, music)
        h.eventually(lambda: ((h.bridge.now_playing or {}).get("queue_item") or {}).get("index") == 8)
    with app.expect_response("**/now-playing/toggle?*") as response:
        player.get_by_role("button", name="Play", exact=True).click()
    result = response.value.json()

    if native_state == "audible":
        assert result == {"ok": True, "paused": False}
        h.eventually(tab_audible)
        assert media.evaluate("(video) => video.currentTime") > 24
        playing = app.request.get(status_url).json()["now_playing"]
        assert (playing["video_id"], playing["duration"]) == (video_id, 6)
        assert 0 < playing["position"] < 6
        assert playing["queue_item"] == {"video_id": video_id, "playlist": "PL_fixture", "index": 7}
        expect(player.get_by_role("button", name="Pause", exact=True)).to_be_enabled()
        h.eventually(lambda: len(lifecycle._plays(h)) == 1)
        assert lifecycle._plays(h)[0]["video_id"] == video_id
        assert music.evaluate("fixture.clicks") == ["playpause"]
    else:
        assert result["ok"] is False
        if native_state in {"other-media", "recent-audio"}:
            assert music.locator("audio").evaluate("audio => audio.paused") is (native_state == "recent-audio")
            if native_state == "other-media":
                assert tab_audible()
        if silent or native_state in {"stalled-clock", "tab-muted"}:
            assert media.evaluate("(video) => video.currentTime") > 24
            music.wait_for_function("document.querySelector('ytmusic-player video').ended")
            lifecycle._sample(h, music)
            assert music.evaluate("fixture.clicks") == ["playpause"]
        else:
            assert media.evaluate("(video) => video.currentTime") == 24
            assert music.evaluate("fixture.clicks") == []
        if native_state == "moved-occurrence":
            # The rejected click reconciles the card without another Play request.
            app.wait_for_function("window.playbackStatus.snapshot?.now_playing?.queue_item?.index === 8")
        app.evaluate("window.playbackStatus.refresh()")
        expect(player.get_by_role("button", name="Play", exact=True)).to_be_enabled()
        assert lifecycle._plays(h) == []
        player_events = h.store.player_events.player_events_since(0)
        assert not [row for row in player_events if row["video_id"] == video_id
                    and row["kind"] in {"tick", "ended", "track_exit"}]
        expect(app.locator("[role=alert]:visible, .toast-err")).to_have_count(0)


@pytest.mark.parametrize("playback_bridge", ["strict"], indirect=True)
@pytest.mark.parametrize("audible", [True, False], ids=["audible-native-start", "silent-native-start"])
def test_blocked_autoplay_is_reported_and_music_activation_recovers(playback_bridge, audible):
    h = playback_bridge
    h.store.tracks.upsert_track("activation", "First song", "Recording artist", None, 30)
    music = h.context.new_page()
    music.goto(f"https://music.youtube.com/?v=activation&audible={str(audible).lower()}")
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
    assert lifecycle._plays(h) == []
    music.get_by_role("button", name="Play or pause", exact=True).click()
    music.wait_for_function("document.querySelector('video').currentTime > .3")
    lifecycle._sample(h, music)
    if not audible:
        h.eventually(lambda: (h.bridge.now_playing or {}).get("paused") is False)
        music.get_by_role("button", name="Play or pause", exact=True).click()
        lifecycle._sample(h, music)
        h.eventually(lambda: (h.bridge.now_playing or {}).get("paused") is True)
        assert lifecycle._plays(h) == []
        assert not [event for event in h.store.player_events.player_events_since(0)
                    if event["video_id"] == "activation" and event["kind"] in {"tick", "ended", "track_exit"}]
        return
    h.eventually(lambda: [row["video_id"] for row in lifecycle._plays(h)] == ["activation"])
    assert inspect("fixture.clicks") == ["playpause", "playpause"]
    app.evaluate("window.playbackStatus.refresh()")
    player.get_by_role("button", name="Pause", exact=True).click()
    expect(player.get_by_role("button", name="Play", exact=True)).to_be_enabled()
    player.get_by_role("button", name="Play", exact=True).click()
    expect(player.get_by_role("button", name="Pause", exact=True)).to_be_enabled()
    expect(app.locator("#playback-error")).to_have_count(0)
