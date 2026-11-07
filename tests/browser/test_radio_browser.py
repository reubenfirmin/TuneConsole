"""Radio's launch, playback-presence gaps, and launcher interactions in the real UI."""
import html
import json
import re

import pytest
from tests.template_support import web_templates
from playwright.sync_api import expect
from tests.browser.radio_ui_support import radio_ui






def test_launch_waits_for_playback_and_playlist_before_revealing_session(page, radio_ui):
    api = radio_ui
    page.locator(".radio-seed").first.click()
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible()
    expect(launch).to_contain_text("Finding your frequency")
    expect(page.locator(".radio-seeds")).to_be_hidden()
    expect(page.locator(".radio-session")).to_be_hidden()
    expect(launch.locator(".gen-node")).to_have_count(8)
    expect(launch).to_have_css("opacity", "1")
    expect(launch.locator("button:visible, a:visible")).to_have_count(0)
    page.screenshot(path="/tmp/tuneconsole-radio-launch.png")

    api.state = {**api.ready, "starting": True}
    api.hold_playlist = True
    assert len(api.starts) == 1
    assert api.starts[0].request.post_data_json["video_id"] == "radio0"
    api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(launch).to_contain_text("Connecting to YouTube Music")
    expect(launch.locator("button:visible, a:visible")).to_have_count(0)
    # Confirmed playback alone cannot reveal a session whose playlist is still loading.
    api.state = api.ready
    with page.expect_request("**/radio/status", timeout=6000):
        pass
    expect(page.locator(".radio-session")).to_be_hidden()
    api.hold_playlist = False
    for route in api.playlists:
        route.fulfill(content_type="text/html", body=api.playlist)
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    expect(launch).to_be_hidden()
    expect(page.locator(".radio-session table")).to_be_visible()




def test_resuming_session_waits_for_history_and_keeps_it_visible_during_refresh(page, radio_ui):
    api = radio_ui
    api.hold_playlist = True
    with page.expect_request("**/radio/playlist", timeout=6000):
        api.state = api.ready
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible(timeout=6000)
    expect(launch).to_contain_text("Returning to your radio")
    expect(launch.locator("button:visible, a:visible")).to_have_count(0)
    session = page.locator('.radio-session')
    expect(session).to_be_hidden()
    expect(page.locator('.radio-directions')).to_be_hidden()
    page.screenshot(path='/tmp/tuneconsole-radio-restoring.png')
    api.hold_playlist = False
    api.playlists.pop().fulfill(content_type='text/html', body=api.playlist)
    expect(session).to_be_visible(timeout=6000)
    expect(session.locator('table')).to_be_visible()
    expect(launch).to_be_hidden()

    # Later metadata refreshes retain the complete session while fetching its table.
    api.hold_playlist = True
    api.state = {**api.ready, 'metadata_revision': 1}
    with page.expect_request("**/radio/playlist", timeout=6000):
        page.evaluate("() => { Alpine.$data(document.querySelector('.radio-home')).poll(); }")
    expect(session).to_be_visible()
    expect(launch).to_be_hidden()
    api.hold_playlist = False
    api.playlists.pop().fulfill(content_type='text/html', body=api.playlist)
    expect(session).to_be_visible()












@pytest.mark.parametrize("failure", ["network"])
def test_polling_failure_preserves_session_and_recovers_quietly(page, radio_ui, failure):
    api = radio_ui
    api.state = api.ready
    expect(page.locator(".radio-session table")).to_be_visible(timeout=6000)

    def unavailable(route):
        if failure == "network":
            route.abort()
        elif failure == "http":
            route.fulfill(status=503)
        else:
            route.fulfill(content_type="application/json", body="temporarily unavailable")

    page.route("**/radio/status", unavailable)
    page.evaluate("() => Alpine.$data(document.querySelector('.radio-home')).poll()")
    expect(page.locator(".radio-session table")).to_be_visible()
    expect(page.locator(".radio-seeds")).to_be_hidden()
    expect(page.locator(".radio-home").get_by_role("alert")).to_be_hidden()

    page.unroute("**/radio/status", unavailable)
    api.state = {"active": False}
    expect(page.locator(".radio-seeds")).to_be_visible(timeout=6000)
    expect(page.locator(".radio-home").get_by_role("alert")).to_be_hidden()
