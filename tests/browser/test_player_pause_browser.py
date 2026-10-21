"""#97: real extension state and polling control the player animation and buttons."""
import json
import re

import pytest
from playwright.sync_api import expect
from websockets.sync.client import connect

from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


@pytest.mark.parametrize("path", ["/", "/artists"])
@pytest.mark.parametrize("width", [1280, 390])
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
        if width < 1040:  # The desktop sidebar intentionally omits the equalizer.
            expect(equalizer).to_be_visible()
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
        assert frame == {"type": "playpause"}
        # Wait for a real status poll after the extension acknowledges resume; this also checks
        # that a subsequent poll does not undo the optimistic click's visual state.
        extension.send(json.dumps({"type": "pevent", "kind": "state", "state": "playing",
                                   "videoId": "v_pause", "position": 21, "duration": 200}))
        with page.expect_response(lambda response: response.url.endswith("/bridge/status")
                                  and response.json().get("now_playing", {}).get("paused") is False,
                                  timeout=8000):
            pass
        expect(widget.get_by_role("button", name="Pause", exact=True)).to_be_visible()
        if width < 1040:
            expect(equalizer).to_be_visible()
        expect(widget.locator(".hs-now")).not_to_have_class(re.compile(r"\bpaused\b"))
        expect(equalizer.locator("i").first).to_have_css("animation-play-state", "running")
