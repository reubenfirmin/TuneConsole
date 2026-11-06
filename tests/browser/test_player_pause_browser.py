"""#97: real extension state and polling control the player animation and buttons."""
import json
import re

import pytest
from playwright.sync_api import expect
from websockets.sync.client import connect

from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


@pytest.mark.parametrize("path", ["/", "/artists"])
@pytest.mark.parametrize("width", [1280])
def test_pause_resume_reaches_global_player(live_app, page, path, width):
    page.set_viewport_size({"width": width, "height": 900})
    with connect(live_app.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps({"type": "play", "videoId": "v_pause", "title": "Pause test",
                                   "artist": "Player artist", "paused": False}))
        page.goto(live_app + path)
        widget = page.locator(".global-now-playing")
        equalizer = widget.locator(".hs-eq")
        expect(widget).to_be_visible()
        expect(widget.get_by_role("button", name="Pause", exact=True)).to_be_visible()
        assert equalizer.locator("i").first.evaluate("el => getComputedStyle(el).animationName") != "none"

        # A pause from YouTube must propagate without another track-start report.
        extension.send(json.dumps({"type": "pevent", "kind": "state", "state": "paused",
                                   "videoId": "v_pause", "position": 20, "duration": 200}))
        expect(widget.get_by_role("button", name="Play", exact=True)).to_be_visible(timeout=8000)
        expect(equalizer).to_be_hidden()
        expect(widget.locator(".hs-now")).to_have_class(re.compile(r"\bpaused\b"))
        expect(equalizer.locator("i").first).to_have_css("animation-play-state", "paused")

        widget.get_by_role("button", name="Play", exact=True).click()
        frame = json.loads(extension.recv(timeout=5))
        while frame == {"ping": 1}:
            frame = json.loads(extension.recv(timeout=5))
        assert frame == {"id": frame["id"], "type": "playpause", "action": "play", "videoId": "v_pause"}
        # A transient absent player is not a resumed acknowledgement. Keep the
        # observer installed across that response and the later real status poll.
        absent = []
        def absent_once(route):
            if not absent:
                absent.append(True)
                route.fulfill(json={"connected": True, "now_playing": None})
            else:
                route.continue_()
        page.route("**/bridge/status", absent_once)

        def resumed(response):
            if not response.url.endswith("/bridge/status"):
                return False
            state = response.json().get("now_playing") or {}
            return state.get("video_id") == "v_pause" and state.get("paused") is False

        with page.expect_response(resumed, timeout=8000):
            with page.expect_response(lambda response: response.url.endswith("/bridge/status")
                                      and response.json().get("now_playing") is None):
                page.evaluate("window.playbackStatus.refresh()")
            assert absent == [True]
            extension.send(json.dumps({"type": "pevent", "kind": "state", "state": "playing",
                                       "videoId": "v_pause", "position": 21, "duration": 200}))
            # A sensor update does not acknowledge this exact control command.
            # Complete the extension protocol before accepting the UI result.
            with page.expect_response(lambda response: '/now-playing/toggle?' in response.url) as toggled:
                extension.send(json.dumps({'id': frame['id'], 'status': 200,
                    'body': json.dumps({'ok': True, 'videoId': 'v_pause', 'paused': False})}))
                page.evaluate("window.playbackStatus.refresh()")
        assert toggled.value.status == 200
        assert toggled.value.json() == {'ok': True, 'paused': False}
        expect(widget).to_be_visible()
        expect(widget.get_by_role("button", name="Pause", exact=True)).to_be_enabled()
        expect(page.locator('#playback-error')).to_have_count(0)
        expect(widget.locator(".hs-now")).not_to_have_class(re.compile(r"\bpaused\b"))
        expect(equalizer.locator("i").first).to_have_css("animation-play-state", "running")


@pytest.mark.parametrize("failure", [None, "network", "invalid_json"])
def test_card_waits_for_confirmation_and_reports_failed_requests(live_app, page, tmp_path, failure):
    status = {"connected": True, "now_playing": {
        "title": "Confirmed song", "artist": "Artist", "video_id": "confirmed", "paused": False}}
    page.route("**/bridge/status", lambda route: route.fulfill(json=status))
    requests = []
    page.route("**/now-playing/toggle?*", lambda route: requests.append(route))
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(live_app + "/artists")
    player = page.locator(".global-now-playing")
    pause = player.get_by_role("button", name="Pause", exact=True)
    pause.click()
    expect(pause).to_be_disabled()
    expect(pause).to_have_attribute("aria-busy", "true")
    assert len(requests) == 1
    assert "action=pause" in requests[0].request.url
    assert "videoId=confirmed" in requests[0].request.url
    page.evaluate("Alpine.$data(document.querySelector('.global-now-playing')).toggle()")
    assert len(requests) == 1, "a pending command cannot be submitted twice"
    if failure == "network":
        requests[0].abort()
    elif failure == "invalid_json":
        requests[0].fulfill(content_type="text/plain", body="unavailable")
    else:
        status["now_playing"]["paused"] = True
        requests[0].fulfill(json={"ok": True, "paused": True})
    if failure:
        expect(pause).to_be_enabled()
        expect(pause).not_to_have_attribute('aria-busy', 'true')
        expect(page.locator('#playback-error, #navigation-error, .toast-err, [role="alert"]:visible')).to_have_count(0)
        assert len(requests) == 1
        pause.click()
        expect(pause).to_be_disabled()
        assert len(requests) == 2
        assert 'action=pause' in requests[1].request.url and 'videoId=confirmed' in requests[1].request.url
        status['now_playing']['paused'] = True
        requests[1].fulfill(json={'ok': True, 'paused': True})
        expect(player.get_by_role('button', name='Play', exact=True)).to_be_enabled()
    else:
        expect(player.get_by_role("button", name="Play", exact=True)).to_be_enabled()
        expect(page.locator("#playback-error")).to_have_count(0)
