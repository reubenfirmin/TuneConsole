"""Radio's launch, playback-presence gaps, and launcher interactions in the real UI."""
import html
import json
import re

import pytest
from tests.template_support import web_templates
from playwright.sync_api import expect
from tests.browser.radio_ui_support import radio_ui


def test_seed_grid_fills_available_space_without_javascript_sizing(page, radio_ui):
    # Disable the measurement callback before Alpine creates the reloaded view.
    # CSS must allocate the initial grid and handle resizing independently.
    def without_sizing(route):
        response = route.fetch()
        route.fulfill(response=response, body=response.text() + '''
          const measuredRadioArtwork = radioArtwork;
          radioArtwork = function() {
            const artwork = measuredRadioArtwork();
            artwork.fitSpace = function() {};
            return artwork;
          };
        ''')

    page.route('**/static/radio-artwork.js*', without_sizing)
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('.radio-seed')).to_have_count(16)
    for width, height in [(1440, 1000), (1440, 700)]:
        page.set_viewport_size({'width': width, 'height': height})
        geometry = page.locator('.radio-seeds').evaluate('''grid => {
          const main = grid.closest('main'), bounds = grid.getBoundingClientRect();
          const page = main.getBoundingClientRect();
          const styles = getComputedStyle(grid);
          return {height: bounds.height, bottom: bounds.bottom,
            expectedBottom: page.bottom - parseFloat(getComputedStyle(main).paddingBottom),
            pageBottom: page.bottom, viewport: innerHeight,
            columns: styles.gridTemplateColumns.split(' ').length,
            rows: styles.gridTemplateRows.split(' ').length,
            overflowingGrid: grid.scrollHeight > grid.clientHeight + 1,
            overflow: document.documentElement.scrollWidth > innerWidth};
        }''')
        assert abs(geometry['bottom'] - geometry['expectedBottom']) < 2, geometry
        # The launcher fills the dashboard's remaining viewport without sizing
        # itself in JavaScript.
        assert geometry['height'] > height / 2, geometry
        assert geometry['pageBottom'] <= height + 12, geometry  # main's entrance animation may translate 10px
        assert geometry['columns'] == 4, geometry
        assert geometry['rows'] == 4, geometry
        assert not geometry['overflowingGrid'], geometry
        assert not geometry['overflow'], geometry
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    expect(page.locator('.radio-seeds')).to_be_hidden()
    page.get_by_role('tab', name='On air', exact=True).click()
    expect(page.locator('.radio-seeds')).to_be_visible()


def test_repair_radio_renders_without_a_local_playlist_record(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, 'local_id': None}
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    expect(page.locator('.radio-session table')).to_be_visible()
    expect(page.get_by_role('status', name='Starting radio')).to_be_hidden()
    expect(page.locator('.radio-seeds')).to_be_hidden()
    page.reload()
    expect(page.locator('.radio-session table')).to_be_visible(timeout=6000)
    expect(page.get_by_role('status', name='Starting radio')).to_be_hidden()


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
    page.screenshot(path="/tmp/tuneconsole-radio-launch.png")

    # Cancel is reachable while the original POST is held. Its late response
    # cannot reveal the old session or clear a subsequent Start's busy state.
    first = api.starts.pop()
    first_id = first.request.post_data_json['launch_id']
    cancel = launch.get_by_role('button', name='Cancel', exact=True)
    expect(cancel).to_be_enabled()
    cancel.focus()
    with page.expect_request('**/radio/stop') as stopping:
        cancel.press('Enter')
    assert stopping.value.post_data_json == {'launch_id': first_id}
    expect(page.locator('.radio-seeds')).to_be_visible()
    expect(launch).to_be_hidden()
    page.locator('.radio-seed').first.click()
    assert len(api.starts) == 1
    assert api.starts[0].request.post_data_json['launch_id'] != first_id
    with page.expect_response('**/radio/start'):
        first.fulfill(json={'ok': True, **api.ready})
    expect(launch).to_contain_text('Finding your frequency')
    expect(page.locator('.radio-session')).to_be_hidden()
    assert page.evaluate("Alpine.$data(document.querySelector('.radio-home')).busy")

    api.state = {**api.ready, "starting": True}
    api.hold_playlist = True
    assert len(api.starts) == 1
    assert api.starts[0].request.post_data_json["video_id"] == "radio0"
    api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(launch).to_contain_text("Connecting to YouTube Music")
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
    expect(launch).to_contain_text("Returning to your radio")
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

    # Restored identified sessions cancel their persisted owner. Older saved
    # sessions still use the ordinary global Stop contract.
    api.hold_playlist = True
    for launch_id in ('restored-launch', None):
        api.state = {**api.ready, 'launch_id': launch_id}
        page.reload(wait_until='domcontentloaded')
        expect(launch).to_be_visible(timeout=6000)
        with page.expect_request('**/radio/stop') as stopping:
            launch.get_by_role('button', name='Cancel', exact=True).click()
        assert stopping.value.post_data_json == {'launch_id': launch_id}
        expect(launch).to_be_hidden()
        expect(page.locator('.radio-seeds')).to_be_visible()
        for route in api.playlists:
            route.fulfill(content_type='text/html', body=api.playlist)
        api.playlists.clear()


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
    expect(launch).to_contain_text("Returning to your radio")
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
def test_resume_is_usable_while_artwork_loads_in_place(page, radio_ui, live_app, outcome):
    api = radio_ui
    pending = []
    url = live_app + '/radio-art/100.svg'
    page.route(url, lambda route: pending.append(route))
    if outcome == 'record':
        api.playlist += f'<svg><image href="{url}" width="100" height="100"/></svg>'
        state = api.ready
    else:
        branches = [{**branch, 'tracks': [
            {**song, 'thumbnail': url} if index == 0 and branch['slot'] == 0 else song
            for index, song in enumerate(branch['tracks'])]} for branch in api.ready['directions']]
        state = {**api.ready, 'directions': branches}
    with page.expect_request(url, timeout=6000):
        api.state = state
        page.evaluate('() => Alpine.$data(document.querySelector(".radio-home")).poll()')
    launch = page.get_by_role('status', name='Starting radio')
    try:
        # The image remains deliberately unresolved while the actual controls
        # and published song list are visible and usable.
        expect(page.locator('.radio-session')).to_be_visible(timeout=3000)
        expect(page.locator('.radio-directions')).to_be_visible()
        expect(page.locator('.radio-session tr[data-vid="radio0"]')).to_be_visible()
        expect(launch).to_be_hidden()
        assert pending
        if outcome in {'loaded', 'record'}:
            for route in pending:
                route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="purple"/></svg>')
            pending.clear()
        elif outcome == 'failed':
            for route in pending:
                route.abort()
            pending.clear()
            expect(page.locator(f'.radio-session img[src="{url}"]')).to_have_count(0)
        expect(page.locator('.radio-session')).to_be_visible()
        expect(launch).to_be_hidden()
    finally:
        for route in pending:
            route.abort()


@pytest.mark.parametrize("source", ["server", "cache"])
def test_restored_table_is_usable_before_its_artwork_finishes(page, radio_ui, live_app, source):
    api = radio_ui
    pending = []
    url = live_app + '/radio-art/101.svg'
    page.route(url, lambda route: pending.append(route))
    api.state = api.ready
    api.playlist = api.playlist.replace('<td>Night frequency 1</td>',
                                        f'<td><img class="pl-thumb" src="{url}" alt="">Night frequency 1</td>')
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
    try:
        expect(page.locator('.radio-session')).to_be_visible(timeout=3000)
        expect(page.get_by_role('status', name='Starting radio')).to_be_hidden()
        image = page.locator(f'.radio-session img[src="{url}"]')
        expect(image).to_be_visible()
        assert not image.evaluate('img => img.complete')
        assert pending
        for route in pending:
            route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>')
        pending.clear()
        page.wait_for_function('url => [...document.images].some(img => img.src === url && img.complete && img.naturalWidth > 0)', arg=url)
        expect(page.locator('.radio-session')).to_be_visible()
    finally:
        for route in pending:
            route.abort()


def test_launch_failure_keeps_its_selection_in_the_existing_loader(page, radio_ui):
    page.locator(".radio-seed").first.click()
    expect(page.locator(".radio-launch")).to_be_visible()
    radio_ui.starts.pop().fulfill(json={"ok": False, "reason": "Connect the YouTube Music extension first."})
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    expect(page.locator('.radio-launch-recovery')).to_have_count(0)
    expect(page.locator('.radio-launch')).to_be_visible()
    expect(page.locator('.radio-launch')).to_contain_text('Night frequency 1')
    expect(page.get_by_role('button', name='Try this selection again')).to_be_enabled()


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


@pytest.mark.parametrize("failure", ["network", "http", "server", "disconnected"])
@pytest.mark.parametrize('width', [1440])
def test_launch_connection_failure_never_exposes_fetch_error(page, radio_ui, failure, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    if failure in ('network', 'http'):
        page.clock.install()
    page.locator(".radio-seed").first.click()
    route = radio_ui.starts.pop()
    if failure == "network":
        route.abort()
    elif failure == 'http':
        route.fulfill(status=503)
    elif failure == 'disconnected':
        route.fulfill(json={'ok': False, 'reason': 'Connect the YouTube Music extension first.'})
    else:
        route.fulfill(json={"ok": False, "reason": "TypeError: Failed to fetch"})
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    expect(page.locator('.radio-launch-recovery')).to_have_count(0)
    message = page.locator('.radio-launch')
    expect(message).to_be_visible()
    expect(message.get_by_role('heading')).to_have_text('Night frequency 1')
    expect(message).to_contain_text('Artist 1')
    expect(message).not_to_contain_text('Failed to fetch')
    expect(message.get_by_role('link')).to_have_attribute('href', 'https://music.youtube.com/watch?v=radio0')
    if failure in ('network', 'http'):
        expect(message).to_contain_text('up to 30 seconds')
        # Inactive status may predate a still-running POST. It cannot authorize
        # another provider create, even after the bounded check has ended.
        page.evaluate("Alpine.$data(document.querySelector('.radio-home')).refresh()")
        page.clock.fast_forward(30001)
        expect(message).to_contain_text('We couldn’t confirm playback')
        page.evaluate("Alpine.$data(document.querySelector('.radio-home')).start({video_id:'radio0'})")
        message.get_by_role('button', name='Check playback').click()
        assert not radio_ui.starts
        expect(page.locator('.radio-seed-start').first).to_be_disabled()
    else:
        expect(page.locator(".radio-seeds")).to_be_hidden()
        expect(page.locator(".radio-seed-start").first).to_be_enabled()
        if failure == 'disconnected':
            expect(message).to_contain_text('connect its extension')
        message.get_by_role('button', name='Try this selection again').click()
        expect(message.locator('.radio-launch-actions')).to_be_hidden()
        assert len(radio_ui.starts) == 1
        radio_ui.starts.pop().fulfill(json={'ok': False})
    page.screenshot(path=f'/tmp/tuneconsole-633-radio-retry-{width}-{failure}.png', full_page=True)
    if failure in ('network', 'http'):
        # Later confirmed output still settles the same launch without a POST.
        radio_ui.state = radio_ui.ready
        page.evaluate("Alpine.$data(document.querySelector('.radio-home')).refresh()")
        expect(page.locator('.radio-session table')).to_be_visible(timeout=6000)
        expect(message).to_be_hidden()
        assert not radio_ui.starts


def test_unknown_launch_cannot_be_settled_by_the_previous_active_session(page, radio_ui):
    radio_ui.state = radio_ui.ready
    page.evaluate("Alpine.$data(document.querySelector('.radio-home')).refresh()")
    expect(page.locator('.radio-session table')).to_be_visible(timeout=6000)
    page.clock.install()
    page.evaluate("() => { Alpine.$data(document.querySelector('.radio-home')).start({video_id:'radio0',title:'Selected song',artist:'Selected artist'}); }")
    expect(page.locator('.radio-launch')).to_be_visible()
    radio_ui.starts.pop().abort()
    message = page.locator('.radio-launch')
    expect(message).to_contain_text('up to 30 seconds')
    page.clock.fast_forward(30001)
    expect(message.get_by_role('heading')).to_have_text('Selected song')
    expect(message).to_contain_text('We couldn’t confirm playback')
    expect(message.get_by_role('button', name='Try this selection again')).to_be_hidden()
    message.get_by_role('button', name='Check playback').click()
    assert not radio_ui.starts

    # A missing Stop receipt and even an inactive poll cannot authorize another
    # creation. Reload retains cancellation, and admitted work must drain first.
    page.route('**/radio/stop', lambda route: route.abort())
    message.get_by_role('button', name='Cancel', exact=True).click()
    expect(message).to_contain_text('haven’t confirmed your Stop request')
    expect(page.locator('.radio-seeds')).to_be_hidden()
    assert page.evaluate("JSON.parse(sessionStorage.getItem('radio-launch')).cancelling")
    page.reload()
    message = page.locator('.radio-launch')
    expect(message).to_contain_text('haven’t confirmed your Stop request')
    expect(page.locator('.radio-seeds')).to_be_hidden()
    old_launch = page.evaluate("JSON.parse(sessionStorage.getItem('radio-launch')).id")
    # Another tab can already own a fresh active session while the old
    # cancelled request drains. Its state must remain intact throughout.
    radio_ui.state = {**radio_ui.ready, 'launch_id': 'another-launch',
                      'stopping': True, 'stopping_launches': [old_launch]}
    page.route('**/radio/stop', lambda route: route.fulfill(json={'ok': True, **radio_ui.state}))
    message.get_by_role('button', name='Cancel', exact=True).click()
    expect(message).to_contain_text('Stopping your radio')
    expect(page.locator('.radio-seed-start').first).to_be_disabled()
    assert not radio_ui.starts
    radio_ui.state = {**radio_ui.ready, 'launch_id': 'another-launch',
                      'stopping': False, 'stopping_launches': []}
    page.evaluate("Alpine.$data(document.querySelector('.radio-home')).refresh()")
    expect(message).to_be_hidden()
    expect(page.locator('.radio-session table')).to_be_visible(timeout=6000)
    assert radio_ui.state['active']
    assert page.evaluate("sessionStorage.getItem('radio-launch')") is None


def test_rejected_playlist_launch_keeps_its_title_and_retry_target(page, radio_ui):
    selected = {'id': 41, 'title': 'Evening records', 'tracks': radio_ui.tracks[:4]}
    page.evaluate("song => { Alpine.$data(document.querySelector('.radio-home')).start(song,true); }", selected)
    expect(page.locator('.radio-launch')).to_be_visible()
    radio_ui.starts.pop().fulfill(json={'ok': False, 'reason': 'No playable songs'})
    message = page.locator('.radio-launch')
    expect(message.get_by_role('heading')).to_have_text('Evening records')
    expect(message.get_by_role('link', name='Back to this playlist')).to_have_attribute('href', '/playlist/41')
    message.get_by_role('button', name='Try this selection again').click()
    assert len(radio_ui.starts) == 1
    retry = radio_ui.starts.pop()
    assert retry.request.post_data_json['playlist_id'] == 41
    assert 'video_id' not in retry.request.post_data_json
    retry.fulfill(json={'ok': False})


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


def test_unconfirmed_launch_keeps_selection_for_retry_instead_of_showing_session(page, radio_ui):
    api = radio_ui
    page.locator(".radio-seed").first.click()
    expect(page.locator(".radio-launch")).to_be_visible()
    api.state = {**api.ready, "starting": True}
    api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(page.locator(".radio-launch")).to_contain_text("Connecting to YouTube Music")
    api.state = {**api.ready, "starting": False, "active": False}
    # A confirmed rejection retains the chosen song in the shared loader. The
    # latest recovery contract offers an explicit retry rather than a seed grid.
    message = page.locator('.radio-launch')
    retry = message.get_by_role('button', name='Try this selection again')
    expect(retry).to_be_visible(timeout=6000)
    expect(retry).to_be_enabled()
    expect(message.get_by_role('heading')).to_have_text('Night frequency 1')
    expect(message).to_contain_text('Artist 1')
    expect(message.get_by_role('link')).to_have_attribute('href', 'https://music.youtube.com/watch?v=radio0')
    expect(page.locator('.radio-seeds')).to_be_hidden()
    expect(page.locator(".radio-session")).to_be_hidden()
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    page.screenshot(path='/tmp/tuneconsole-830-unconfirmed-retry.png', full_page=True)
    assert not api.starts
    retry.click()
    assert len(api.starts) == 1
    request = api.starts.pop()
    assert request.request.post_data_json['video_id'] == 'radio0'
    assert 'playlist_id' not in request.request.post_data_json
    request.fulfill(json={'ok': False, 'reason': 'No playable songs'})
    expect(retry).to_be_enabled()
    expect(page.locator('.radio-session')).to_be_hidden()


@pytest.mark.parametrize('width,height', [(1280, 520), (1280, 720), (1440, 1000)])
def test_launcher_highlight_keyboard_and_pointer(page, radio_ui, width, height):
    page.set_viewport_size({"width": width, "height": height})
    seed = page.locator(".radio-seed").first
    seed.get_by_role('button').focus()
    expect(seed.locator(".radio-seed-info")).to_have_css("opacity", "1")
    expect(seed.locator(".radio-seed-launch")).to_have_css("opacity", "1")
    assert seed.locator(".radio-seed-play").bounding_box()["width"] >= 40
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    seed.hover()
    page.screenshot(path=f"/tmp/tuneconsole-radio-launcher-{width}-{height}.png")
    expect(page.locator(".radio-seed").nth(1)).to_have_css("opacity", "0.55")
    page.locator(".radio-seed").nth(1).hover()
    expect(page.locator(".radio-seed").nth(1)).to_have_css("opacity", "1")
    seed.hover()
    page.emulate_media(reduced_motion="reduce")
    seed.get_by_role('button').press("Enter")
    expect(page.locator(".radio-launch")).to_be_visible()
    node = page.locator(".radio-launch .gen-node").first
    start = node.evaluate("el => el.style.transform")
    page.wait_for_function("start => document.querySelector('.radio-launch .gen-node').style.transform !== start", arg=start)
    expect(page.locator(".radio-launch .gen-hub-ring")).to_have_css("animation-play-state", "running")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize('height', [520, 720])
def test_short_desktop_play_control_and_song_link_are_separate(page, radio_ui, live_app, height):
    page.set_viewport_size({'width': 1280, 'height': height})
    seed = page.locator('.radio-seed').first
    play = seed.locator('.radio-seed-play')
    link = seed.get_by_role('link')
    play.hover()
    expect(seed.locator('.radio-seed-info')).to_have_css('opacity', '1')
    geometry = seed.evaluate('''seed => {
      const bounds = node => node.getBoundingClientRect().toJSON();
      return {tile: bounds(seed), play: bounds(seed.querySelector('.radio-seed-play')),
        link: bounds(seed.querySelector('.radio-seed-info a'))};
    }''')
    tile, control, title = (geometry[key] for key in ('tile', 'play', 'link'))
    assert control['width'] >= 40 and control['height'] >= 40, geometry
    assert tile['left'] <= control['left'] and control['right'] <= tile['right'], geometry
    assert tile['top'] <= control['top'] and control['bottom'] <= tile['bottom'], geometry
    assert control['right'] <= title['left'] or control['bottom'] <= title['top'], geometry
    page.screenshot(path=f'/tmp/tuneconsole-1075-covers-1280-{height}.png')
    # Follow the real song link, then return and launch with the visible play
    # circle. Disabling the link's pointer events must not satisfy this check.
    target = link.get_attribute('href')
    link.click()
    expect(page).to_have_url(f'{live_app}{target}')
    assert not radio_ui.starts
    page.goto(f'{live_app}/#radio')
    play.hover()
    play.click()
    expect(page.get_by_role('status', name='Starting radio')).to_be_visible()
    assert len(radio_ui.starts) == 1
    request = radio_ui.starts.pop()
    assert request.request.post_data_json['video_id'] == radio_ui.tracks[0]['video_id']
    request.fulfill(json={'ok': False, 'reason': 'No playable songs'})


@pytest.mark.parametrize("width", [1440])
def test_playlist_radio_button_starts_through_transition_without_relaunching_on_reload(
        page, radio_ui, store, live_app, width):
    api = radio_ui
    source = store.playlists.get_playlists()[0]
    ids = [store.tracks.upsert_track(track["video_id"], track["title"], track["artist"], "", 200,
                                     thumbnail=track["thumbnail"]) for track in api.tracks[:4]]
    store.playlists.set_playlist_tracks(source.id, ids)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_app}/playlist/{source.id}")
    play = page.get_by_role("button", name="Play on YouTube Music", exact=True)
    radio = page.get_by_role("link", name="Start radio from this playlist", exact=True)
    expect(play).to_be_visible()
    expect(play.locator("svg")).to_have_count(1)
    expect(radio).to_be_visible()
    expect(play).to_be_in_viewport(ratio=1)
    expect(radio).to_be_in_viewport(ratio=1)
    radio_box, play_box = radio.bounding_box(), play.bounding_box()
    assert radio_box["x"] > play_box["x"] + play_box["width"]
    assert radio_box["width"] < play_box["width"]
    assert abs(radio_box["y"] + radio_box["height"] / 2
               - play_box["y"] - play_box["height"] / 2) < 1
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
    expect(launch).to_contain_text("Connecting to YouTube Music")
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


