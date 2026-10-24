"""Return from the global player to its source without issuing a playback command."""
import json
import re

import pytest
from playwright.sync_api import expect
from websockets.sync.client import connect

from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


def test_space_controls_playback_without_intercepting_search_or_focused_buttons(live_app, page):
    status = {'connected':True,'now_playing':{'video_id':'keyboard','title':'Song','artist':'Artist','paused':False}}
    page.route('**/bridge/status',lambda route: route.fulfill(json=status))
    commands = []
    def toggle(route):
        commands.append(route.request.url)
        status['now_playing']['paused'] = not status['now_playing']['paused']
        route.fulfill(json={'ok':True})
    page.route('**/now-playing/toggle',toggle)
    page.goto(live_app+'/artists')
    player = page.locator('.global-now-playing')
    expect(player).to_be_visible()
    page.locator('body').click(position={'x':600,'y':600})
    page.keyboard.press('Space')
    expect(player.get_by_role('button',name='Play',exact=True)).to_be_visible()
    assert len(commands)==1
    search = page.get_by_role('searchbox',name='Search your library')
    search.fill('two')
    search.press('Space')
    expect(search).to_have_value('two ')
    assert len(commands)==1
    player.get_by_role('button',name='Play',exact=True).focus()
    page.keyboard.press('Space')
    expect(player.get_by_role('button',name='Pause',exact=True)).to_be_visible()
    assert len(commands)==2


@pytest.mark.parametrize('path,width', [('/',1280),('/artists',1280),('/',390),('/artists',390),('/artists',800)])
def test_progress_uses_control_divider_without_time_row_in_home_and_dock(live_app, page, path, width, tmp_path):
    status = {'connected':True,'now_playing':{'video_id':'progress','title':'Song','artist':'Artist',
        'position':45,'duration':200,'paused':True}}
    page.route('**/bridge/status',lambda route: route.fulfill(json=status))
    page.set_viewport_size({'width':width,'height':900})
    page.emulate_media(reduced_motion='reduce')
    page.clock.install()
    page.goto(live_app+path)
    player = page.locator('.global-now-playing')
    progress = player.get_by_role('progressbar',name='Track progress')
    expect(progress).to_be_visible()
    expect(progress).to_have_attribute('value','45')
    expect(progress).to_have_attribute('max','200')
    expect(progress).to_have_attribute('aria-valuetext','0:45 of 3:20')
    expect(progress).to_have_attribute('title','0:45 of 3:20')
    expect(player.locator('.hs-times')).to_have_count(0)
    line = progress.locator('..')
    assert line.get_attribute('class') == 'hs-progress'
    assert line.locator('..').get_attribute('class') == 'hs-rate-group'
    assert line.evaluate("el => getComputedStyle(el, '::after').boxShadow") != 'none'
    assert line.evaluate("el => getComputedStyle(el).getPropertyValue('--played')").strip() == '22.5%'
    box, card = progress.bounding_box(), player.bounding_box()
    assert box['height'] <= 2
    assert box['x']>=card['x'] and box['x']+box['width']<=card['x']+card['width']
    if width != 800:
        controls = player.locator('.hs-rate-group').bounding_box()
        assert abs(box['y'] - controls['y']) <= 1
    page.screenshot(path=tmp_path/f'progress-{path.strip("/") or "home"}-{width}.png')
    status['now_playing'].update(position=3,duration=None)
    page.evaluate('window.playbackStatus.refresh()')
    expect(progress).to_be_hidden()
    expect(player).not_to_contain_text('--:--')
    page.emulate_media(forced_colors='active')
    status['now_playing'].update(duration=200)
    page.evaluate('window.playbackStatus.refresh()')
    expect(progress).to_be_visible()
    assert progress.evaluate('el => getComputedStyle(el).forcedColorAdjust')=='none'
    # Reduced motion still displays locally estimated progress between heartbeats.
    status['now_playing'].update(position=3,paused=False)
    page.evaluate('window.playbackStatus.refresh()')
    page.clock.run_for(500)
    assert 3.3 < float(progress.get_attribute('value')) <= 3.5
    status['now_playing'].update(position=4,paused=True)
    page.evaluate('window.playbackStatus.refresh()')
    page.clock.run_for(500)
    expect(progress).to_have_attribute('value','4')


@pytest.mark.parametrize("kind", ["playlist", "album"])
def test_track_table_and_dock_share_one_playback_request_per_interval(live_app, store, page, kind):
    playlist = store.playlists.get_playlists()[0]
    store.collection.add_saved_album({"browse": "MPRE_shared", "title": "Shared album", "artist": "Artist"})
    tracks = [store.tracks.upsert_track(video, title, "Artist", "Shared album", 200,
                                      album_browse_id="MPRE_shared")
              for video, title in (("shared-one", "First song"), ("shared-two", "Second song"))]
    store.playlists.set_playlist_tracks(playlist.id, tracks)
    path = f"/playlist/{playlist.id}" if kind == "playlist" else "/album?browse=MPRE_shared"
    status = {"connected": True, "now_playing": {
        "video_id": "shared-one", "title": "First song", "artist": "Artist", "paused": False}}
    requests = []

    def respond(route):
        requests.append(route.request.url)
        route.fulfill(json=status)

    page.route("**/bridge/status", respond)
    page.clock.install()
    page.goto(live_app + path)
    dock = page.locator(".global-now-playing")
    expect(dock).to_contain_text("First song")
    expect(page.locator('tr[data-vid="shared-one"]')).to_have_class(re.compile(r"\bnow-playing\b"))
    assert len(requests) == 1
    status["now_playing"].update(video_id="shared-two", title="Second song")
    page.clock.run_for(2600)
    expect(dock).to_contain_text("Second song")
    expect(page.locator('tr[data-vid="shared-two"]')).to_have_class(re.compile(r"\bnow-playing\b"))
    expect(page.locator('tr[data-vid="shared-one"]')).not_to_have_class(re.compile(r"\bnow-playing\b"))
    assert len(requests) == 2
    with page.expect_response("**/bridge/status"):
        page.clock.run_for(2500)
    assert len(requests) == 3


@pytest.mark.parametrize("width", [1280, 390])
def test_player_controls_are_aligned_and_next_sends_the_displayed_song(live_app, page, width, tmp_path):
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {"title": "A song with a long title to fit in the player",
            "artist": "Artist", "video_id": "radio-song", "paused": False, "likeStatus": "INDIFFERENT",
            "thumbnail": "/static/nag/intro.png",
            "source": {"kind": "radio", "title": "Radio Session", "url": "/#radio"}},
    }))
    page.route("**/now-playing/next", lambda route: route.fulfill(json={"ok": True}))
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_app + "/artists")
    player = page.locator(".global-now-playing")
    expect(player).to_be_visible()
    controls = player.locator(".hs-rate-group button")
    assert controls.count() == 4
    boxes = [button.bounding_box() for button in controls.all()]
    assert all(abs(box["y"] + box["height"] / 2 - boxes[0]["y"] - boxes[0]["height"] / 2) < 1
               for box in boxes)
    assert all(left["x"] + left["width"] <= right["x"] for left, right in zip(boxes, boxes[1:]))
    card = player.bounding_box()
    assert all(box["x"] >= card["x"] and box["x"] + box["width"] <= card["x"] + card["width"] for box in boxes)
    with page.expect_request("**/now-playing/next") as request:
        player.get_by_role("button", name="Next / skip").click()
    assert request.value.post_data_json == {"videoId": "radio-song"}
    expect(player.locator(".hs-control-error")).to_have_count(0)
    page.screenshot(path=tmp_path / f"now-playing-{width}.png")


@pytest.mark.parametrize("outcome", ["rating_failed", "player_failed", "skip_failed"])
def test_player_keeps_failures_out_of_the_card_without_losing_saved_dislike(live_app, page, outcome):
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {"title": "Song", "artist": "Artist",
            "video_id": "radio-song", "paused": False, "likeStatus": "INDIFFERENT"},
    }))
    result = {"ok": outcome != "rating_failed", "error": "Player unavailable"}
    if outcome == "player_failed":
        result["player_error"] = "Please try Next."
    page.route("**/now-playing/rate", lambda route: route.fulfill(json=result))
    page.route("**/now-playing/next", lambda route: route.fulfill(json={"ok": False}))
    page.goto(live_app + "/artists")
    player = page.locator(".global-now-playing")
    if outcome == "skip_failed":
        player.get_by_role("button", name="Next / skip").click()
    else:
        with page.expect_request("**/now-playing/rate") as request:
            player.get_by_role("button", name="Dislike and skip").click()
        assert request.value.post_data_json == {"action": "dislike", "videoId": "radio-song", "likeStatus": "DISLIKE"}
    expect(player.locator(".hs-next")).to_be_enabled()
    expect(player.locator(".hs-control-error, [role=status], [role=alert]")).to_have_count(0)
    expect(player).not_to_contain_text("Player unavailable")
    expect(player).not_to_contain_text("Please try Next")
    expect(page.locator(".toast-err")).to_have_count(0)
    if outcome == "player_failed":
        expect(player.locator(".hs-skull")).to_have_class(re.compile(r"\bon\b"))
    elif outcome == "rating_failed":
        expect(player.locator(".hs-skull")).not_to_have_class(re.compile(r"\bon\b"))


@pytest.mark.parametrize("path", ["/", "/artists"])
@pytest.mark.parametrize("width", [1280, 390])
def test_playback_favicon_and_radio_tab_follow_confirmed_playback(live_app, store, page, path, width):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    status = {"connected": True, "radio": True, "radio_ready": True, "now_playing": {
        "title": "Playing song", "artist": "Artist", "video_id": "v1", "paused": False,
        "source": {"kind": "radio", "url": "/#radio", "title": "Radio Session"},
    }}
    page.route("**/bridge/status", lambda route: route.fulfill(json=status))
    page.route("**/radio/status", lambda route: route.fulfill(json={"active": True, "starting": False}))
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": []}))
    page.set_viewport_size({"width": width, "height": 900})
    page.clock.install()
    page.goto(live_app + path + ("#radio" if path == "/" else ""))
    icon = page.locator('link[rel="icon"]')
    playing_icon = re.compile(r"/static/favicon-playing\.svg\?v=.+")
    idle_icon = re.compile(r"/static/favicon\.svg\?v=.+")
    expect(icon).to_have_attribute("href", playing_icon)
    assert page.request.get(live_app + icon.get_attribute("href")).ok
    dot = page.locator(".dash-on-air-dot")

    def refresh():
        with page.expect_response("**/bridge/status"):
            page.clock.run_for(3100)

    if path == "/":
        tab = page.get_by_role("tab", name="On air", exact=True)
        expect(dot).to_be_visible()
        expect(tab).to_have_attribute("aria-description", "Radio playing")
        playing_color = dot.evaluate("el => getComputedStyle(el).backgroundColor")
        assert dot.evaluate("el => getComputedStyle(el).animationName") == "none"
        page.emulate_media(reduced_motion="reduce")
        assert dot.evaluate("el => getComputedStyle(el).animationName") == "none"
        expect(dot).to_be_visible()

    # Saved mixes and albums keep the speaker, even with a radio session still active.
    for kind in ("playlist", "album"):
        status["now_playing"]["source"]["kind"] = kind
        refresh()
        expect(icon).to_have_attribute("href", playing_icon)
        if path == "/":
            expect(dot).to_be_hidden()

    status["now_playing"]["source"]["kind"] = "radio"
    for paused in (True, None):
        status["now_playing"]["paused"] = paused
        refresh()
        expect(icon).to_have_attribute("href", idle_icon)
        if path == "/":
            if paused is True:
                expect(dot).to_be_visible()
                expect(dot).to_have_class("dash-on-air-dot is-paused")
                expect(tab).to_have_attribute("aria-description", "Radio paused")
                assert dot.evaluate("el => getComputedStyle(el).backgroundColor") != playing_color
                assert dot.evaluate("el => { const probe = document.createElement('span'); "
                                    "probe.style.color = 'var(--dim)'; el.append(probe); "
                                    "const gray = getComputedStyle(probe).color; probe.remove(); "
                                    "return getComputedStyle(el).backgroundColor === gray; }")
            else:
                expect(dot).to_be_hidden()

    status["now_playing"]["paused"] = False
    status["connected"] = False
    refresh()
    expect(icon).to_have_attribute("href", idle_icon)
    if path == "/":
        expect(dot).to_be_hidden()
    status["connected"] = True
    refresh()
    expect(icon).to_have_attribute("href", playing_icon)
    if path == "/":
        expect(dot).to_be_visible()
        expect(dot).to_have_class("dash-on-air-dot")
        expect(tab).to_have_attribute("aria-description", "Radio playing")
        assert dot.evaluate("el => getComputedStyle(el).backgroundColor") == playing_color
    status["now_playing"] = None
    refresh()
    expect(icon).to_have_attribute("href", idle_icon)
    if path == "/":
        expect(dot).to_be_hidden()


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("now_playing", [
    None,
    {"video_id": "v1", "paused": False,
     "source": {"kind": "radio", "url": "/#radio"}},
])
def test_radio_has_no_playback_dot_without_now_playing(live_app, store, page, now_playing, active):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "radio": True, "radio_ready": active, "now_playing": now_playing,
    }))
    page.route("**/radio/status", lambda route: route.fulfill(json={"active": active}))
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": [
        {"video_id": "seed", "title": "Start here", "artist": "Artist"},
    ]}))
    page.goto(live_app + "/#radio")
    if not active:
        expect(page.get_by_role("button", name="Start radio with Start here by Artist")).to_be_visible()
    expect(page.locator(".global-now-playing")).to_be_hidden()
    expect(page.locator(".dash-on-air-dot")).to_be_hidden()
    expect(page.get_by_role("tab", name="On air", exact=True)).not_to_have_attribute(
        "aria-description", "Radio playing")


@pytest.mark.parametrize("starting", [False, True])
def test_radio_launcher_does_not_light_dot_from_an_old_playback_report(live_app, store, page, starting):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "radio": starting, "radio_ready": False, "now_playing": {
            "title": "Previous song", "artist": "Artist", "video_id": "old", "paused": False,
            "source": {"kind": "radio", "url": "/#radio"},
        },
    }))
    page.route("**/radio/status", lambda route: route.fulfill(json={"active": starting, "starting": starting}))
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": []}))
    page.goto(live_app + "/#radio")
    expect(page.locator(".global-now-playing")).to_be_visible()
    expect(page.locator(".dash-on-air-dot")).to_be_hidden()


@pytest.mark.parametrize("failure", ["network", "invalid_response"])
def test_playback_dot_and_sidebar_clear_when_status_refresh_fails(live_app, store, page, failure):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    unavailable = False

    def bridge_status(route):
        if unavailable:
            if failure == "network":
                route.abort()
            else:
                route.fulfill(status=503, content_type="text/plain", body="Unavailable")
        else:
            route.fulfill(json={"connected": True, "radio": True, "radio_ready": True, "now_playing": {
                "title": "Playing song", "artist": "Artist", "video_id": "v1", "paused": False,
                "source": {"kind": "radio", "url": "/#radio"},
            }})

    page.route("**/bridge/status", bridge_status)
    page.route("**/radio/status", lambda route: route.fulfill(json={"active": True, "starting": False}))
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": []}))
    page.clock.install()
    page.goto(live_app + "/#radio")
    widget = page.locator(".global-now-playing")
    dot = page.locator(".dash-on-air-dot")
    expect(widget).to_be_visible()
    expect(dot).to_be_visible()
    unavailable = True
    page.clock.run_for(3100)
    expect(widget).to_be_hidden()
    expect(dot).to_be_hidden()
    expect(page.locator('link[rel="icon"]')).to_have_attribute("href", re.compile(r"/static/favicon\.svg\?v=.+"))
    expect(page.get_by_role("heading", name="Connect the extension")).to_be_hidden()
    unavailable = False
    page.clock.run_for(3100)
    expect(widget).to_be_visible()
    expect(dot).to_be_visible()


@pytest.mark.parametrize("panel", ["recent", "directions"])
def test_radio_dot_stays_current_without_opening_the_radio_panel(live_app, store, page, panel):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    status = {"connected": True, "radio": True, "radio_ready": True, "now_playing": {
        "title": "Playing song", "artist": "Artist", "video_id": "v1", "paused": False,
        "source": {"kind": "radio", "url": "/#radio", "title": "Radio Session"},
    }}
    # The unvisited panel has an old view. The shared playback response is authoritative.
    page.route("**/bridge/status", lambda route: route.fulfill(json=status))
    page.route("**/radio/status", lambda route: route.fulfill(json={"active": False}))
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": []}))
    page.clock.install()
    page.goto(live_app + "/#" + panel)
    dot = page.locator(".dash-on-air-dot")
    expect(page.get_by_role("link", name="Back to Radio: Radio Session")).to_be_visible()
    expect(dot).to_be_visible()
    status["now_playing"]["paused"] = True
    page.clock.run_for(3100)
    expect(dot).to_have_class("dash-on-air-dot is-paused")
    status["now_playing"] = None
    page.clock.run_for(3100)
    expect(dot).to_be_hidden()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("path", ["/", "/artists"])
def test_now_playing_uses_cached_artwork_and_clears_it_on_track_change(live_app, store, page, width, path):
    cached_art = "/static/nag/intro.png"
    store.tracks.upsert_track("v_art", "Song", "Artist", "Album", 200, thumbnail=cached_art)
    page.set_viewport_size({"width": width, "height": 900})
    with connect(live_app.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        def playing(video_id, thumbnail=""):
            extension.send(json.dumps({"type": "now-heartbeat", "videoId": video_id,
                                       "title": "Song", "artist": "Artist", "paused": True,
                                       "thumbnail": thumbnail}))

        playing("v_art")
        page.goto(live_app + path)
        artwork = page.locator(".global-now-playing .hs-art")
        expect(artwork).to_be_visible()
        expect(artwork).to_be_in_viewport()
        expect(artwork).to_have_attribute("src", cached_art)
        expect(artwork).to_have_js_property("complete", True)
        assert artwork.evaluate("image => image.naturalWidth") > 0
        if width == 1280:
            expect(page.locator(".sidebar-dock-slot .hs-art")).to_be_visible()

        # Matching names alone must not carry the preceding upload's artwork forward.
        playing("unknown")
        expect(artwork).to_be_hidden(timeout=8000)
        playing("v_art", "/static/nag/lastfm.png")
        expect(artwork).to_have_attribute("src", "/static/nag/lastfm.png", timeout=8000)
        expect(artwork).to_be_visible()


@pytest.mark.parametrize("width", [1280, 390])
def test_now_playing_returns_to_radio_and_switches_home_tabs(live_app, store, page, width):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "radio": True,
        "now_playing": {"title": "Song", "artist": "Artist", "video_id": "v1", "paused": False,
                        "source": {"kind": "radio", "title": "Radio Session", "url": "/#radio", "external": False}},
    }))
    page.route("**/radio/status", lambda route: route.fulfill(json={"active": True, "starting": False}))
    page.route("**/radio/seeds", lambda route: route.fulfill(json={"tracks": []}))
    requests = []
    page.on("request", lambda request: requests.append(request.url) if request.url.endswith("/play") else None)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_app}/artists")
    widget = page.locator(".global-now-playing")
    link = widget.get_by_role("link", name="Back to Radio: Radio Session")
    expect(link).to_have_text("Back to Radio")
    link.click()
    expect(page).to_have_url(f"{live_app}/#radio")
    expect(page.get_by_role("tab", name="On air", exact=True)).to_have_attribute("aria-selected", "true")
    expect(widget.locator(".hs-source")).to_have_count(0)
    page.get_by_role("tab", name="Mix tapes", exact=True).click()
    expect(link).to_be_visible()
    link.click()
    expect(page.get_by_role("tab", name="On air", exact=True)).to_have_attribute("aria-selected", "true")
    expect(widget.locator(".hs-source")).to_have_count(0)
    assert requests == []


@pytest.mark.parametrize("kind", ["playlist", "album"])
@pytest.mark.parametrize("width", [1280, 390])
def test_now_playing_returns_to_source(live_app, store, page, kind, width):
    playlist = store.playlists.get_playlists()[0]
    store.collection.add_saved_album({"browse": "MPRE_album", "title": "Playing release", "artist": "Artist"})
    store.tracks.upsert_track("v1", "Song", "Artist", "Playing release", 200, album_browse_id="MPRE_album")
    url = f"/playlist/{playlist.id}" if kind == "playlist" else "/album?browse=MPRE_album"
    title = playlist.title if kind == "playlist" else "Playing release"
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True,
        "now_playing": {"title": "Song", "artist": "Artist", "video_id": "v1", "paused": True,
                        "source": {"kind": kind, "title": title, "url": url, "external": False}},
    }))
    playback_requests = []
    page.on("request", lambda request: playback_requests.append(request.url)
            if request.url.endswith("/play") else None)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_app}/artists")
    widget = page.locator(".global-now-playing")
    link = widget.get_by_role("link", name=f"Back to {kind}: {title}")
    expect(link).to_be_visible()
    expect(link).to_be_in_viewport()
    expect(link.locator("svg")).to_be_visible()
    if width == 1280:
        expect(page.locator(".sidebar-dock-slot .hs-source")).to_be_visible()
    link.click()
    expect(page).to_have_url(f"{live_app}{url}")
    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    expect(widget).to_be_visible()
    expect(widget.locator(".hs-source")).to_have_count(0)
    # Sorting or jumping to a section still leaves us on the playing source.
    separator = "&" if "?" in url else "?"
    page.goto(f"{live_app}{url}{separator}sort=title#tracks")
    expect(widget).to_be_visible()
    expect(widget.locator(".hs-source")).to_have_count(0)
    # Another playlist, or another album sharing the /album route, still needs the link.
    if kind == "playlist":
        other = store.playlists.upsert_playlist(playlist.identity_id, "PL_other", "Another mix", 0, "h", 0)
        page.goto(f"{live_app}/playlist/{other}")
    else:
        page.goto(f"{live_app}/album?browse=MPRE_other")
    expect(link).to_be_visible()
    assert playback_requests == []


@pytest.mark.parametrize("source", [
    None,
    {"kind": "playlist", "title": "Playing playlist", "external": True,
     "url": "https://music.youtube.com/playlist?list=PL_unknown"},
    {"kind": "playlist", "title": "Playing playlist",
     "url": "https://music.youtube.com/playlist?list=PL_unknown"},
])
def test_now_playing_without_local_source_has_no_source_link(live_app, page, source):
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {"title": "Song", "artist": "Artist", "source": source},
    }))
    page.goto(f"{live_app}/playlists")
    expect(page.locator(".global-now-playing")).to_be_visible()
    expect(page.locator(".hs-source")).to_have_count(0)


@pytest.mark.parametrize("failure", ["disconnected", "network", "invalid_response"])
def test_failed_playback_does_not_open_an_uncontrolled_music_tab(live_app, store, page, failure):
    requests = []
    recovered = False

    def play(route):
        requests.append(route.request.post_data_json)
        if recovered:
            route.fulfill(json={"ok": True})
        elif failure == "network":
            route.abort()
        elif failure == "invalid_response":
            route.fulfill(status=503, content_type="text/plain", body="Unavailable")
        else:
            route.fulfill(json={"ok": False, "error": "no extension connected"})

    page.route("**/play", play)
    playlist = store.playlists.get_playlists()[0]
    page.goto(f"{live_app}/playlist/{playlist.id}")
    control = page.get_by_role("link", name="Play on YouTube Music")
    control.click()
    error = page.locator("#playback-error")
    expect(error).to_contain_text("Couldn’t start playback")
    assert len(page.context.pages) == 1
    assert requests == [{"url": f"https://music.youtube.com/playlist?list={playlist.ytm_playlist_id}"}]
    # The existing play control works on retry and clears the error once the bridge recovers.
    recovered = True
    control.click()
    expect(error).to_have_count(0)
    assert len(requests) == 2
    assert len(page.context.pages) == 1
