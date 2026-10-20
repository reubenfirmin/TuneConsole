"""Exercise the recap against a packaged server with the real idle-shutdown monitor."""
import multiprocessing
import os
import socket
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.test_trends_recap_route import _client, _STORY, _detailed_story


def _serve_recap(port, data_dir):
    os.environ["YT_PLAYLIST_EXIT_ON_IDLE"] = "1"
    os.environ["YT_PLAYLIST_HOME"] = data_dir
    client, _ = _client(_STORY)
    uvicorn.run(client.app, host="127.0.0.1", port=port, log_level="warning")


@pytest.fixture
def packaged_recap(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    process = multiprocessing.get_context("spawn").Process(
        target=_serve_recap, args=(port, str(tmp_path)))
    process.start()
    try:
        deadline = time.monotonic() + 15
        while True:
            assert process.is_alive(), "packaged server exited before startup"
            try:
                with urlopen(base + "/bridge/status", timeout=0.2):
                    break
            except URLError:
                assert time.monotonic() < deadline, "packaged server did not start"
                time.sleep(0.02)
        yield base, process
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)


def test_recap_keeps_packaged_server_alive_through_download_and_exit(packaged_recap, page, tmp_path):
    base, process = packaged_recap
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.goto(base + "/")
    page.get_by_role("tab", name="Needs attention", exact=True).click()
    expect(page.locator("#recap-nudge")).to_be_visible()
    # Leaving Home sends /goodbye. The standalone reel must cancel it immediately and continue
    # heartbeating, or the production monitor kills the server after its 2.5-second grace period.
    with page.expect_response(lambda r: r.url == base + "/heartbeat" and r.status == 204):
        page.locator('#recap-nudge a[href="/trends/story/2026-06"]').click()
    page.wait_for_timeout(3500)  # pass the real monitor's shutdown grace while the reel is open
    assert process.is_alive()
    page.get_by_role("button", name=f"Beat {len(_STORY['beats'])}", exact=True).click()
    save = page.get_by_role("button", name="Save image", exact=True)
    expect(save).to_be_visible()
    with page.expect_download() as download_info:
        save.click()
    download = download_info.value
    assert download.suggested_filename == "your-june.png"
    image = tmp_path / download.suggested_filename
    download.save_as(image)
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert page.url == base + "/trends/story/2026-06"
    with page.expect_response(lambda r: r.url == base + "/heartbeat" and r.status == 204):
        page.get_by_role("link", name="Close", exact=True).click()
    assert page.url == base + "/"
    expect(page.locator("#recap-nudge")).to_have_count(0)
    assert process.is_alive()
    assert errors == []
    # Retain normal packaged behavior: closing the actual UI tab should still stop the server.
    page.close()
    process.join(timeout=8)
    assert not process.is_alive()


def test_detail_links_open_without_advancing_the_recap(live_app, store, page):
    story = _detailed_story()
    store.put_proposals("trend_rollups", {"story": story}, 1)
    page.goto(live_app + "/trends/story/2026-06")
    index = next(i for i, b in enumerate(story["beats"]) if b["kind"] == "new_this_month")
    page.get_by_role("button", name=f"Beat {index + 1}", exact=True).click()
    slide = page.locator(".slide-new_this_month")
    expect(slide).to_have_css("opacity", "1")
    # Use a fragment to observe the link's default navigation without leaving the reel.
    link = slide.get_by_role("link", name="An album with a long title", exact=True)
    link.evaluate("el => el.href = '#album-preview'")
    link.click()
    assert page.url.endswith("#album-preview")
    expect(slide).to_be_visible()
    # Keyboard navigation remains available without dedicated Back / Continue buttons.
    page.keyboard.press("ArrowRight")
    expect(page.locator(".slide-different")).to_be_visible()
    page.keyboard.press("ArrowLeft")
    expect(slide).to_be_visible()


@pytest.mark.parametrize("viewport", [{"width": 390, "height": 844}, {"width": 844, "height": 390}])
def test_optional_screens_fit_and_navigate_on_mobile(live_app, store, page, viewport, tmp_path):
    story = _detailed_story()
    store.put_proposals("trend_rollups", {"story": story}, 1)
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.set_viewport_size(viewport)
    page.goto(live_app + "/trends/story/2026-06")
    expect(page.get_by_role("button", name="Back", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Continue", exact=True)).to_have_count(0)
    for kind in ("new_this_month", "different", "same"):
        index = next(i for i, b in enumerate(story["beats"]) if b["kind"] == kind)
        page.get_by_role("button", name=f"Beat {index + 1}", exact=True).click()
        slide = page.locator(f".slide-{kind}")
        expect(slide).to_be_visible()
        expect(slide).to_have_css("opacity", "1")
        expect(slide.get_by_role("heading", level=1)).to_be_in_viewport()
        panel = slide.locator(".recap-details")
        bounds = panel.bounding_box()
        assert bounds["height"] > 0
        assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= viewport["height"]
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= viewport["width"]
        panel.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        expect(panel.locator(".recap-entries li, .recap-findings li").last).to_be_in_viewport()
        panel.evaluate("el => { el.scrollTop = 0; }")
        page.screenshot(path=str(tmp_path / f"recap-{kind}-{viewport['width']}.png"))
        # Tapping the panel's padding advances just like tapping the background.
        panel.click(position={"x": bounds["width"] * .75, "y": 5})
        expect(page.locator(f".slide-{story['beats'][index + 1]['kind']}")).to_be_visible()
        page.mouse.click(10, viewport["height"] * .6)
        expect(slide).to_be_visible()
        expect(slide).to_have_css("opacity", "1")
        # The left side of a panel also follows the normal back-tap convention.
        panel.click(position={"x": 20, "y": 5})
        expect(page.locator(f".slide-{story['beats'][index - 1]['kind']}")).to_be_visible()
    assert errors == []
