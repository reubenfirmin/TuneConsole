"""Return from the global player to its source without issuing a playback command."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("kind", ["playlist", "album"])
@pytest.mark.parametrize("width", [1280, 390])
def test_now_playing_returns_to_source(live_app, store, page, kind, width):
    playlist = store.get_playlists()[0]
    store.add_saved_album({"browse": "MPRE_album", "title": "Playing release", "artist": "Artist"})
    store.upsert_track("v1", "Song", "Artist", "Playing release", 200, album_browse_id="MPRE_album")
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
        other = store.upsert_playlist(playlist.identity_id, "PL_other", "Another mix", 0, "h", 0)
        page.goto(f"{live_app}/playlist/{other}")
    else:
        page.goto(f"{live_app}/album?browse=MPRE_other")
    expect(link).to_be_visible()
    assert playback_requests == []


def test_now_playing_without_context_has_no_source_link(live_app, page):
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {"title": "Song", "artist": "Artist", "source": None},
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
    playlist = store.get_playlists()[0]
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
