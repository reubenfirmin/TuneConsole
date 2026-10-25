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
    expect(launch).to_contain_text("finding your frequency")
    expect(page.locator(".radio-seeds")).to_be_hidden()
    expect(page.locator(".radio-session")).to_be_hidden()
    expect(launch.locator(".radio-launch-node")).to_have_count(8)
    expect(launch).to_have_css("opacity", "1")
    page.screenshot(path="/tmp/tuneconsole-radio-launch.png")

    api.state = {**api.ready, "starting": True}
    api.hold_playlist = True
    assert len(api.starts) == 1
    assert api.starts[0].request.post_data_json["video_id"] == "radio0"
    api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(launch).to_contain_text("connecting to youtube music")
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


def test_active_session_ignores_bridge_gaps_and_new_radio_is_in_options(page, radio_ui):
    api = radio_ui
    api.state = api.ready
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    for report in ({"connected": False}, {"connected": True, "now_playing": None},
                   {"connected": True, "now_playing": {"paused": True}}):
        page.evaluate("data => window.dispatchEvent(new CustomEvent('bridge-status', {detail: data}))", report)
        expect(page.locator(".radio-session")).to_be_visible()
        expect(page.locator(".radio-seeds")).to_be_hidden()
    page.get_by_role("tab").first.click()
    page.get_by_role("tab", name="On air", exact=True).click()
    expect(page.locator(".radio-session")).to_be_visible()
    expect(page.get_by_role("button", name="New radio", exact=True)).to_be_hidden()
    options = page.locator(".radio-menu > summary")
    expect(page.locator("thead .radio-menu > summary")).to_have_count(1)
    expect(page.locator(".radio-heading")).to_have_count(0)
    options.press("Enter")
    expect(page.get_by_role("button", name="New radio", exact=True)).to_be_visible()
    options.press("Escape")
    expect(page.get_by_role("button", name="New radio", exact=True)).to_be_hidden()
    options.click()
    page.get_by_role("button", name="New radio", exact=True).click()
    expect(page.locator(".radio-seeds")).to_be_visible()
    assert api.stops == ["POST"]


def test_resuming_session_waits_for_history_and_keeps_it_visible_during_refresh(page, radio_ui):
    api = radio_ui
    api.hold_playlist = True
    with page.expect_request("**/radio/playlist", timeout=6000):
        api.state = api.ready
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible(timeout=6000)
    expect(launch).to_contain_text("returning to your frequency")
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


def test_restart_waits_for_extension_and_join_before_showing_radio(page, radio_ui, live_app):
    api = radio_ui
    api.state = {**api.ready, "active": False, "resolving": True}

    def restoring_home(route):
        response = route.fetch()
        bootstrap = html.escape(json.dumps({"state": api.state, "seeds": api.tracks}), quote=True)
        body = re.sub(r'x-data="radioHome\([^\"]*\)"',
                      lambda _: f'x-data="radioHome({bootstrap})"', response.text())
        route.fulfill(response=response, body=body)

    page.route(live_app + "/", restoring_home)
    page.reload()
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible()
    expect(launch).to_contain_text("returning to your frequency")
    expect(page.locator('.radio-seeds')).to_be_hidden()
    expect(page.locator('.radio-session')).to_be_hidden()
    api.hold_playlist = True
    with page.expect_request("**/radio/playlist", timeout=6000):
        api.state = api.ready
        page.evaluate('() => { Alpine.$data(document.querySelector(".radio-home")).poll(); }')
    expect(launch).to_be_visible()
    expect(page.locator('.radio-directions')).to_be_hidden()
    api.hold_playlist = False
    for route in api.playlists:
        route.fulfill(content_type="text/html", body=api.playlist)
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    expect(launch).to_be_hidden()


@pytest.mark.parametrize("cached", [False, True])
def test_resume_does_not_accept_a_table_with_missing_history(page, radio_ui, live_app, cached):
    api = radio_ui
    complete = api.playlist
    api.playlist = '<table><thead><tr><th>Song</th></tr></thead><tbody></tbody></table>'
    if cached:
        # A refresh must validate cached rows too, before claiming this session is ready.
        key = json.dumps([1, 0, ['radio0']], separators=(',', ':'))
        page.evaluate('data => sessionStorage.setItem("radio-view", JSON.stringify(data))',
                      {"playlistVersion": 2, "playlistKey": key, "playlistHtml": api.playlist, "seeds": api.tracks})

        def cached_home(route):
            response = route.fetch()
            bootstrap = html.escape(json.dumps({"state": api.ready, "seeds": api.tracks}), quote=True)
            body = re.sub(r'x-data="radioHome\([^\"]*\)"',
                          lambda _: f'x-data="radioHome({bootstrap})"', response.text())
            route.fulfill(response=response, body=body)

        page.route(live_app + "/", cached_home)
        api.state = api.ready
        page.reload()
    else:
        with page.expect_request("**/radio/playlist", timeout=6000):
            api.state = api.ready
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible()
    expect(page.locator('.radio-directions')).to_be_hidden()
    api.playlist = complete
    page.evaluate('() => { Alpine.$data(document.querySelector(".radio-home")).poll(); }')
    expect(page.locator('.radio-session tr[data-vid="radio0"]')).to_be_visible(timeout=6000)
    expect(launch).to_be_hidden()


@pytest.mark.parametrize("outcome", ["loaded", "failed", "stalled", "record"])
def test_resume_waits_for_artwork_or_uses_its_placeholder(page, radio_ui, live_app, outcome):
    api = radio_ui
    pending = []
    url = live_app + '/radio-art/100.svg'
    page.route(url, lambda route: pending.append(route))
    if outcome == 'record':
        api.playlist += f'<svg><image href="{url}" width="100" height="100"/></svg>'
        api.state = api.ready
    else:
        branches = [{**branch, 'tracks': [
            {**song, 'thumbnail': url} if index == 0 and branch['slot'] == 0 else song
            for index, song in enumerate(branch['tracks'])]} for branch in api.ready['directions']]
        api.state = {**api.ready, 'directions': branches}
    launch = page.get_by_role('status', name='Starting radio')
    expect(launch).to_be_visible(timeout=6000)
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).preparingKey !== ""')
    expect(page.locator('.radio-session')).to_be_hidden()
    expect(page.locator('.radio-directions')).to_be_hidden()
    assert pending
    if outcome in {'loaded', 'record'}:
        for route in pending:
            route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="purple"/></svg>')
    elif outcome == 'failed':
        for route in pending:
            route.abort()
    expect(page.locator('.radio-session')).to_be_visible(timeout=11000)
    expect(launch).to_be_hidden()
    if outcome in {'failed', 'stalled'}:
        expect(page.locator(f'.radio-session img[src="{url}"]')).to_have_count(0)
    else:
        assert page.locator('.radio-session img').evaluate_all('images => images.every(img => img.complete && img.naturalWidth > 0)')
    if outcome == 'stalled':
        for route in pending:
            route.abort()


@pytest.mark.parametrize("source", ["server", "cache"])
def test_restored_table_waits_for_its_artwork(page, radio_ui, live_app, source):
    api = radio_ui
    pending = []
    url = live_app + '/radio-art/101.svg'
    page.route(url, lambda route: pending.append(route))
    api.state = api.ready
    api.playlist = api.playlist.replace('<td>Night frequency 1</td>',
                                        f'<td><img src="{url}" alt="">Night frequency 1</td>')
    if source == 'cache':
        key = json.dumps([1, 0, ['radio0']], separators=(',', ':'))
        page.evaluate('data => sessionStorage.setItem("radio-view", JSON.stringify(data))',
                      {"playlistVersion": 2, "playlistKey": key, "playlistHtml": api.playlist, "seeds": api.tracks})

    def restored_home(route):
        response = route.fetch()
        bootstrap = html.escape(json.dumps({"state": api.ready, "seeds": api.tracks}), quote=True)
        body = re.sub(r'x-data="radioHome\([^\"]*\)"',
                      lambda _: f'x-data="radioHome({bootstrap})"', response.text())
        if source == 'server':
            empty = '<div class="radio-playlist" x-ref="playlist"></div>'
            assert empty in body
            body = body.replace(empty, f'<div class="radio-playlist" x-ref="playlist">{api.playlist}</div>')
        route.fulfill(response=response, body=body)

    page.route(live_app + '/', restored_home)
    page.reload(wait_until='domcontentloaded')
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).preparingKey !== ""')
    expect(page.get_by_role('status', name='Starting radio')).to_be_visible()
    expect(page.locator('.radio-session')).to_be_hidden()
    assert pending
    for route in pending:
        route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>')
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    expect(page.get_by_role('status', name='Starting radio')).to_be_hidden()
    assert page.locator(f'.radio-session img[src="{url}"]').evaluate('img => img.complete && img.naturalWidth > 0')


def test_launch_failure_returns_to_song_choices_quietly(page, radio_ui):
    page.locator(".radio-seed").first.click()
    expect(page.locator(".radio-launch")).to_be_visible()
    radio_ui.starts.pop().fulfill(json={"ok": False, "reason": "Connect the YouTube Music extension first."})
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    expect(page.locator(".radio-seeds")).to_be_visible()
    expect(page.locator(".radio-launch")).to_be_hidden()
    expect(page.locator(".radio-seed").first).to_be_enabled()


@pytest.mark.parametrize("failure", ["network", "http", "json"])
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


def test_playlist_fetch_failure_keeps_transition_until_retry_succeeds(page, radio_ui):
    api = radio_ui
    page.locator(".radio-seed").first.click()
    api.state = api.ready
    page.route("**/radio/playlist", lambda route: route.abort())
    with page.expect_event("requestfailed", predicate=lambda request: request.url.endswith("/radio/playlist")):
        api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(page.locator(".radio-launch")).to_be_visible()
    expect(page.locator(".radio-session")).to_be_hidden()
    expect(page.locator(".radio-home").get_by_role("alert")).to_be_hidden()
    page.unroute("**/radio/playlist")
    page.route("**/radio/playlist", lambda route: route.fulfill(content_type="text/html", body=api.playlist))
    expect(page.locator(".radio-session table")).to_be_visible(timeout=6000)
    expect(page.locator(".radio-launch")).to_be_hidden()
    expect(page.locator(".radio-home").get_by_role("alert")).to_be_hidden()


@pytest.mark.parametrize("failure", ["network", "server"])
def test_launch_connection_failure_never_exposes_fetch_error(page, radio_ui, failure):
    page.locator(".radio-seed").first.click()
    route = radio_ui.starts.pop()
    if failure == "network":
        route.abort()
    else:
        route.fulfill(json={"ok": False, "reason": "TypeError: Failed to fetch"})
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    expect(page.locator(".radio-seeds")).to_be_visible()
    expect(page.locator(".radio-seed").first).to_be_enabled()


def test_new_radio_connection_failure_preserves_session_quietly(page, radio_ui):
    radio_ui.state = radio_ui.ready
    expect(page.locator(".radio-session table")).to_be_visible(timeout=6000)
    page.route("**/radio/stop", lambda route: route.abort())
    page.locator(".radio-menu > summary").click()
    page.get_by_role("button", name="New radio", exact=True).click()
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    expect(page.locator(".radio-session table")).to_be_visible()


def test_history_refreshes_for_native_song_missing_from_saved_queue(page, radio_ui):
    api = radio_ui
    template = web_templates().get_template('_partials/radio_playlist.html')
    api.state = {**api.ready, 'history': api.tracks[:2], 'history_count': 2}
    api.playlist = template.render(session=api.state, tracks=api.state['history'])
    rows = page.locator('.radio-history tr')
    expect(rows).to_have_count(2, timeout=6000)
    expect(rows.last).to_have_attribute('data-vid', 'radio1')

    # The saved queue and history length stay unchanged. Only observed playback
    # tells the page it needs different rows and a different playing highlight.
    api.state = {**api.state, 'history': [api.tracks[0], api.tracks[9]],
                 'browser_age': 0, 'browser': {'player': {
                     'playlist_ytm_id': 'PLRADIO', 'video_id': 'radio9'}}}
    api.playlist = template.render(session=api.state, tracks=api.state['history'])
    page.evaluate('() => Alpine.$data(document.querySelector(".radio-home")).poll()')
    expect(rows.last).to_have_attribute('data-vid', 'radio9')
    expect(rows.last).to_have_attribute('aria-current', 'true')


def test_unconfirmed_launch_returns_to_launcher_instead_of_showing_session(page, radio_ui):
    api = radio_ui
    page.locator(".radio-seed").first.click()
    expect(page.locator(".radio-launch")).to_be_visible()
    api.state = {**api.ready, "starting": True}
    api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(page.locator(".radio-launch")).to_contain_text("connecting to youtube music")
    api.state = {**api.ready, "starting": False, "active": False}
    expect(page.locator(".radio-seeds")).to_be_visible(timeout=6000)
    expect(page.locator(".radio-session")).to_be_hidden()
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)


@pytest.mark.parametrize("width,touch", [(1440, False), (390, True)])
def test_launcher_highlight_keyboard_and_touch_layout(page, radio_ui, width, touch):
    page.set_viewport_size({"width": width, "height": 1000})
    if touch:
        session = page.context.new_cdp_session(page)
        session.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
    seed = page.locator(".radio-seed").first
    seed.get_by_role('button').focus()
    expect(seed.locator(".radio-seed-info")).to_have_css("opacity", "1")
    expect(seed.locator(".radio-seed-launch")).to_have_css("opacity", "1")
    assert seed.locator(".radio-seed-play").bounding_box()["width"] >= 40
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    seed.hover()
    page.screenshot(path=f"/tmp/tuneconsole-radio-launcher-{width}.png")
    if not touch:
        expect(page.locator(".radio-seed").nth(1)).to_have_css("opacity", "0.55")
        page.locator(".radio-seed").nth(1).hover()
        expect(page.locator(".radio-seed").nth(1)).to_have_css("opacity", "1")
        seed.hover()
    page.emulate_media(reduced_motion="reduce")
    seed.get_by_role('button').press("Enter")
    expect(page.locator(".radio-launch")).to_be_visible()
    expect(page.locator(".radio-launch-node").first).to_have_css("animation-name", "radio-float")
    expect(page.locator(".radio-launch-node").first).to_have_css("animation-play-state", "running")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_playlist_radio_button_starts_through_transition_without_relaunching_on_reload(
        page, radio_ui, store, live_app, width):
    api = radio_ui
    source = store.playlists.get_playlists()[0]
    ids = [store.tracks.upsert_track(track["video_id"], track["title"], track["artist"], "", 200,
                                     thumbnail=track["thumbnail"]) for track in api.tracks[:4]]
    store.playlists.set_playlist_tracks(source.id, ids)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_app}/playlist/{source.id}")
    play = page.get_by_role("link", name="Play on YouTube Music")
    radio = page.get_by_role("link", name="Start radio from this playlist", exact=True)
    expect(play).to_be_visible()
    expect(play.locator("svg")).to_have_count(0)
    expect(radio).to_be_visible()
    expect(play).to_be_in_viewport(ratio=1)
    expect(radio).to_be_in_viewport(ratio=1)
    radio_box, play_box = radio.bounding_box(), play.bounding_box()
    assert radio_box["x"] > play_box["x"] + play_box["width"]
    assert radio_box["width"] < play_box["width"]
    assert abs(radio_box["height"] - play_box["height"]) < 1
    assert abs(radio_box["width"] - radio_box["height"]) < 1
    page.screenshot(path=f"/tmp/tuneconsole-playlist-radio-{width}.png")
    radio.click()
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible()
    expect(launch).to_contain_text(source.title)
    expect(page.locator(".radio-session")).to_be_hidden()
    expect(page).to_have_url(f"{live_app}/#radio")
    assert len(api.starts) == 1
    data = api.starts[0].request.post_data_json
    assert data["playlist_id"] == source.id
    assert "video_id" not in data
    api.state = {**api.ready, "starting": True}
    api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(launch).to_contain_text("connecting to youtube music")
    with page.expect_response(lambda response: response.url.endswith('/radio/status')
                              and response.json().get('starting') is False, timeout=6000):
        api.state = api.ready
    # Reveal can wait for the existing eight-second artwork fallback after playback
    # is confirmed. The launch contract does not require artwork to load in six seconds.
    expect(page.locator(".radio-session")).to_be_visible(timeout=11000)
    page.reload()
    expect(page.locator(".radio-session")).to_be_visible(timeout=11000)
    assert api.starts == []


def test_missing_playlist_launch_falls_back_to_song_choices(page, radio_ui, live_app):
    page.goto(f"{live_app}/?radio_playlist=999999#radio")
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    expect(page.locator(".radio-seeds")).to_be_visible()
    assert radio_ui.starts == []


