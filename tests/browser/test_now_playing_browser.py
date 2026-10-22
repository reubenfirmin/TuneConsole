"""Return from the global player to its source without issuing a playback command."""
import json

import pytest
from playwright.sync_api import expect
from websockets.sync.client import connect

from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


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
    expect(page.get_by_role("tab", name="Radio", exact=True)).to_have_attribute("aria-selected", "true")
    expect(widget.locator(".hs-source")).to_have_count(0)
    page.get_by_role("tab", name="Listen", exact=True).click()
    expect(link).to_be_visible()
    link.click()
    expect(page.get_by_role("tab", name="Radio", exact=True)).to_have_attribute("aria-selected", "true")
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
