"""Radio's launch, playback-presence gaps, and launcher interactions in the real UI."""
from types import SimpleNamespace

import pytest
from fastapi.templating import Jinja2Templates
from playwright.sync_api import expect


@pytest.fixture
def radio_ui(live_app, store, page):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    tracks = [{"video_id": f"radio{i}", "title": f"Night frequency {i + 1}",
               "artist": f"Artist {i + 1}", "thumbnail": f"{live_app}/radio-art/{i}.svg"}
              for i in range(16)]
    api = SimpleNamespace(state={"active": False}, starts=[], playlists=[], stops=[],
                          hold_playlist=False, errors=[], tracks=tracks)
    api.ready = {"active": True, "starting": False, "playlist_id": "PLRADIO", "local_id": 1,
                 "tracks": tracks[:3], "history_count": 1, "title": "Radio Session",
                 "directions": [{"slot": i, "id": f"direction{i}", "active": i == 0,
                                 "label": ["Stay here", "Deeper", "Brighter", "Further"][i],
                                 "tracks": tracks[i + 1:i + 4]} for i in range(4)]}
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
    options = templates.get_template('_partials/radio_options.html').render()
    api.playlist = ('<div class="radio-track-scroll"><table><thead><tr><th>Song</th><th>' + options +
                    '</th></tr></thead><tbody><tr data-vid="radio0"><td>Night frequency 1</td></tr></tbody></table></div>')

    def playlist(route):
        if api.hold_playlist:
            api.playlists.append(route)
        else:
            route.fulfill(content_type="text/html", body=api.playlist)

    def stop(route):
        api.stops.append(route.request.method)
        api.state = {"active": False}
        route.fulfill(json={"ok": True, **api.state})

    def art(route):
        index = int(route.request.url.rsplit("/", 1)[1].split(".")[0])
        hue = index * 29
        route.fulfill(content_type="image/svg+xml", body=f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
          <defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="hsl({hue} 50% 25%)"/><stop offset="1" stop-color="hsl({hue + 70} 60% 55%)"/></linearGradient></defs>
          <path fill="url(#g)" d="M0 0h300v300H0z"/><circle cx="150" cy="150" r="95" fill="none" stroke="white" stroke-opacity=".25" stroke-width="28"/>
          <circle cx="150" cy="150" r="22" fill="white" fill-opacity=".65"/></svg>''')

    page.on("pageerror", lambda error: api.errors.append(str(error)))
    page.route("**/radio-art/*.svg", art)
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": tracks, "menu_id": "seeds"}))
    page.route("**/radio/status", lambda route: route.fulfill(json=api.state))
    page.route("**/radio/start", lambda route: api.starts.append(route))
    page.route("**/radio/playlist", playlist)
    page.route("**/radio/stop", stop)
    page.route("**/radio/seen", lambda route: route.fulfill(json={"ok": True}))
    page.route("**/bridge/status", lambda route: route.fulfill(json={"connected": True, "now_playing": None}))
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{live_app}/#radio")
    expect(page.locator(".radio-seed")).to_have_count(16)
    yield api
    assert not api.errors


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


@pytest.mark.parametrize("width", [1440, 390])
def test_tune_streams_modal_applies_all_axes_and_keeps_selected_stream_lit(page, radio_ui, width):
    api = radio_ui
    page.set_viewport_size({"width": width, "height": 1000})
    api.state = {**api.ready, "tuning": {"comfort": .3, "theme": .7, "energy": .2}}
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    selected = page.locator(".radio-direction.is-active")
    before = selected.evaluate("el => getComputedStyle(el).boxShadow")
    assert before != "none"
    selected.hover()
    selected.evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
    assert selected.evaluate("el => getComputedStyle(el).boxShadow") == before
    button = page.get_by_role("button", name="Tune streams", exact=True)
    expect(button).to_have_text("")
    expect(button).to_have_css("width", "64px")
    expect(button).to_have_css("height", "64px")
    expect(button).to_have_css("border-radius", "50%")
    expect(button).to_have_css("z-index", "0")
    backing = page.locator(".radio-junction-backing")
    expect(backing).to_have_css("fill-opacity", "1")
    assert backing.evaluate("el => getComputedStyle(el).fill") == page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor")
    button.hover()
    expect(button).to_have_css("z-index", "2")
    button.click()
    dialog = page.get_by_role("dialog", name="Tune streams")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_css("opacity", "1")
    expect(dialog.locator("p, small, .radio-tuning-drawing")).to_have_count(0)
    comfort = dialog.get_by_role("slider", name="Discovery to Comfort")
    theme = dialog.get_by_role("slider", name="Eclectic to Themed")
    energy = dialog.get_by_role("slider", name="Chill to Full On")
    expect(comfort).to_have_value("30")
    expect(theme).to_have_value("70")
    expect(energy).to_have_value("20")
    expect(comfort).to_have_css("background-image", "none")
    assert dialog.bounding_box()["width"] <= width
    page.screenshot(path=f"/tmp/tuneconsole-radio-tuning-{width}.png")
    dialog.get_by_role("button", name="Reset", exact=True).click()
    expect(comfort).to_have_value("50")
    comfort.focus()
    comfort.press("Home")
    expect(comfort).to_have_value("0")
    comfort.press("End")
    expect(comfort).to_have_value("100")
    expect(comfort).to_have_css("background-image", "none")
    comfort.fill("10")
    theme.fill("90")
    energy.fill("80")
    sent = []
    def tune(route):
        sent.append(route.request.post_data_json)
        api.state = {**api.state, "tuning": sent[-1]}
        route.fulfill(json={"ok": True, **api.state})
    page.route("**/radio/tune", tune)
    dialog.get_by_role("button", name="Apply", exact=True).click()
    expect(dialog).to_be_hidden()
    assert sent == [{"comfort": .1, "theme": .9, "energy": .8}]
    expect(page.locator(".radio-session")).to_be_visible()
    button.click()
    expect(comfort).to_have_value("10")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(button).to_be_focused()


def test_decay_and_metadata_refresh_without_switching_streams(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, "directions": [
        {**b, "decay_total": 3, "decay_remaining": 3 if b["active"] else b["slot"],
         "tracks": [{**t, "genre": "ambient", "year": "2001"} for t in b["tracks"]]}
        for b in api.ready["directions"]]}
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    expect(page.locator(".radio-proposed-meta").first).to_contain_text("ambient · 2001")
    bars = page.get_by_role("progressbar")
    expect(bars).to_have_count(4)
    expect(bars.nth(1)).to_have_attribute("aria-valuenow", "1")
    api.state = {**api.state, "directions": [
        {**b, "decay_remaining": 0} if b["slot"] == 1 else b for b in api.state["directions"]]}
    page.evaluate("() => Alpine.$data(document.querySelector('.radio-home')).poll()")
    expect(bars.nth(1)).to_have_attribute("aria-valuenow", "0")
    expect(page.locator(".radio-direction.is-active")).to_have_attribute("data-slot", "0")


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
    seed.focus()
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
    seed.press("Enter")
    expect(page.locator(".radio-launch")).to_be_visible()
    expect(page.locator(".radio-launch-node").first).to_have_css("animation-name", "none")
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
    api.state = api.ready
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    page.reload()
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    assert api.starts == []


def test_missing_playlist_launch_falls_back_to_song_choices(page, radio_ui, live_app):
    page.goto(f"{live_app}/?radio_playlist=999999#radio")
    expect(page.locator(".radio-home").get_by_role("alert")).to_have_count(0)
    expect(page.locator(".radio-seeds")).to_be_visible()
    assert radio_ui.starts == []


@pytest.mark.parametrize('width,height', [(1440, 1000), (1792, 1398), (390, 1000)])
def test_session_history_rises_from_junction_and_queues_preview_without_switching(page, radio_ui, width, height):
    api = radio_ui
    page.set_viewport_size({'width': width, 'height': height})
    api.ready = {**api.ready, 'directions': [
        {**branch, 'tracks': api.tracks[branch['slot'] + 1:branch['slot'] + 7]}
        for branch in api.ready['directions']]}
    templates = Jinja2Templates(directory='src/yt_playlist/web/templates')
    template = templates.get_template('_partials/radio_playlist.html')

    def playing(count):
        tracks = [{**track, 'album': 'Night album', 'album_browse': None,
                   'year': '2001', 'genre': 'ambient', 'duration': 230, 'liked': False}
                  for track in api.tracks]
        api.state = {**api.ready, 'tracks': tracks, 'history_count': count, 'position': count - 1,
                     'browser_age': 0, 'browser': {'player': {
                         'playlist_ytm_id': 'PLRADIO', 'video_id': tracks[count - 1]['video_id']}}}
        api.playlist = template.render(session=api.state, tracks=tracks)

    playing(1)
    session = page.locator('.radio-session')
    expect(session).to_be_visible(timeout=6000)
    rows = session.locator('.radio-history tr')
    expect(rows).to_have_count(1)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    expect(session.get_by_role('columnheader', name='Year', exact=True)).to_have_count(1)
    expect(session.get_by_role('columnheader', name='Genre', exact=True)).to_have_count(1)
    expect(rows.last).to_contain_text('2001')
    expect(rows.last).to_contain_text('ambient')
    assert session.locator('.radio-empty-row').count() >= 2
    scroller = session.locator('.radio-track-scroll')
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    expect(scroller).to_have_css('border-bottom-width', '0px')
    expect(session.locator('.radio-direction-label')).to_have_count(0)
    expect(session.locator('.radio-queue-art img')).to_have_count(12)
    drawing = session.locator('.radio-history-art')
    expect(drawing).to_be_visible()
    assert drawing.bounding_box()['height'] > 80
    assert abs(drawing.bounding_box()['y'] + drawing.bounding_box()['height'] - (rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'])) < 3
    expect(session.locator('.track-table')).to_have_css('opacity', '1')
    assert '.85' in rows.first.evaluate('el => getComputedStyle(el).backgroundColor')
    expect(drawing.locator('.radio-record-cover')).to_have_attribute('href', api.tracks[0]['thumbnail'])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-radio-drawing-{width}.png')

    line = session.locator('.radio-junction-line')
    original = line.evaluate('el => getComputedStyle(el).d')
    other = session.locator('.radio-direction').nth(1)
    other.hover()
    expect(other).to_have_class('radio-direction is-preview')
    page.wait_for_function('(original) => getComputedStyle(document.querySelector(".radio-junction-line")).d !== original', arg=original)
    line.evaluate('el => Promise.all(el.getAnimations().map(animation => animation.finished))')
    expect(session.locator('.radio-direction.is-active')).to_have_attribute('data-slot', '0')
    assert session.locator('.radio-direction').nth(0).evaluate('el => getComputedStyle(el).borderColor') != other.evaluate('el => getComputedStyle(el).borderColor')
    page.screenshot(path=f'/tmp/tuneconsole-radio-session-{width}.png')
    page.mouse.move(0, 0)
    other.get_by_role('button').focus()
    expect(other).to_have_class('radio-direction is-preview')
    page.locator('.radio-menu > summary').focus()
    expect(other).not_to_have_class('radio-direction is-preview')
    page.emulate_media(reduced_motion='reduce')
    other.get_by_role('button').focus()
    expect(line).to_have_css('transition-duration', '0s')
    page.locator('.radio-menu > summary').focus()

    playing(3)
    expect(rows).to_have_count(3, timeout=6000)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    expect(drawing.locator('.radio-record-cover')).to_have_attribute('href', api.tracks[2]['thumbnail'])
    playing(12)
    expect(rows).to_have_count(12, timeout=6000)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    expect(session.locator('.radio-empty-row')).to_have_count(0)
    expect(drawing).to_be_visible()
    assert drawing.bounding_box()['height'] > 80
    assert scroller.evaluate('el => el.scrollTop') > 0
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    # Polling must leave the user free to scroll back through earlier tracks.
    scroller.evaluate('el => el.scrollTop = 0')
    with page.expect_response('**/radio/status', timeout=6000):
        pass
    page.evaluate('() => new Promise(requestAnimationFrame)')
    assert scroller.evaluate('el => el.scrollTop') == 0
