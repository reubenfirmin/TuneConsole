"""The same song menu works across views; playlist writes and modal focus stay predictable."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.test_song_actions import SONG, seed_song_actions


@pytest.fixture
def song_app(store):
    data = seed_song_actions(store)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(data["app"], host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(.02)
    yield {**data, "base": f"http://127.0.0.1:{port}"}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
def test_create_from_album_with_readable_input_and_modal_focus(song_app, page, width, tmp_path):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 850})
    page.goto(song_app["base"] + "/album?browse=MPRE_new")
    trigger = page.get_by_role("button", name=f"More actions for {SONG['title']}")
    trigger.click()
    menu = page.get_by_role("menu", name="Song actions")
    expect(menu).to_be_visible()
    assert page.locator("#song-action-menu").count() == 1
    expect(menu.get_by_role("menuitem", name="Remove from playlist")).to_have_count(0)
    bounds = menu.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] + bounds["height"] <= 850
    page.screenshot(path=tmp_path / f"song-menu-{width}.png")
    menu.get_by_role("menuitem", name="Create playlist from song").click()
    dialog = page.get_by_role("dialog", name="Create playlist", exact=True)
    name = dialog.get_by_label("Playlist name")
    expect(name).to_be_focused()
    expect(name).to_have_value(SONG["title"])
    name.fill("Evening favorites")
    styles = name.evaluate("el => ({text: getComputedStyle(el).color, bg: getComputedStyle(el).backgroundColor})")
    assert styles["text"] != styles["bg"]
    # Native modality prevents both Tab and attempted programmatic focus reaching the page behind.
    for _ in range(8):
        page.keyboard.press("Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    trigger.evaluate("el => el.focus()")
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.screenshot(path=tmp_path / f"song-dialog-{width}.png", animations="disabled")
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()
    assert not song_app["main"].created
    trigger.click()
    menu.get_by_role("menuitem", name="Create playlist from song").click()
    name.fill("Evening favorites")
    dialog.get_by_role("button", name="Create playlist", exact=True).click()
    expect(dialog.get_by_text("Playlist created.", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Create playlist", exact=True)).to_be_hidden()
    expect(dialog.get_by_role("heading", name="Evening favorites", exact=True)).to_be_visible()
    page.screenshot(path=tmp_path / f"song-created-{width}.png", animations="disabled")
    dialog.get_by_role("link", name="Open playlist").click()
    expect(page.get_by_role("heading", name="Evening favorites", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=SONG["title"] + " ↗", exact=True)).to_be_visible()
    assert len(song_app["main"].created) == 1
    assert song_app["main"].added[0][1] == [SONG["video_id"]]
    assert not errors


def test_add_search_error_retry_and_duplicate_state(song_app, page, tmp_path):
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    trigger = page.get_by_role("button", name="More actions for Owned Song")
    trigger.click()
    page.get_by_role("menuitem", name="Add song to playlist", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add to playlist", exact=True)
    expect(dialog.get_by_role("radio", name="Source mix Main Already added")).to_be_disabled()
    expect(dialog.get_by_text("Offline mix", exact=True)).to_have_count(0)
    search = dialog.get_by_role("searchbox", name="Find a playlist")
    expect(search).to_be_focused()
    search.fill("doesn't exist")
    expect(dialog.get_by_text("No matching playlists.")).to_be_visible()
    search.fill("evening")
    dialog.get_by_role("radio", name="Evening mix Other account").check()
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 850})
        expect(dialog.get_by_role("button", name="Add song", exact=True)).to_be_in_viewport()
        page.screenshot(path=tmp_path / f"song-destination-{width}.png", animations="disabled")
    page.route("**/songs/add-to-playlist", lambda route: route.fulfill(
        status=502, json={"detail": "Couldn't add the song. Please try again."}))
    dialog.get_by_role("button", name="Add song", exact=True).click()
    expect(dialog.get_by_role("alert")).to_have_text("Couldn't add the song. Please try again.")
    expect(dialog.get_by_role("radio", name="Evening mix Other account")).to_be_checked()
    page.unroute("**/songs/add-to-playlist")
    dialog.get_by_role("button", name="Add song", exact=True).click()
    expect(dialog.get_by_text("Song added.", exact=True)).to_be_visible()
    page.screenshot(path=tmp_path / "song-added-390.png", animations="disabled")
    assert song_app["other"].added == [("DEST", ["owned-song"])]
    assert not song_app["main"].added
    dialog.get_by_role("button", name="Done").click()
    expect(trigger).to_be_focused()
    trigger.click()
    page.get_by_role("menuitem", name="Add song to playlist", exact=True).click()
    expect(dialog.get_by_role("radio", name="Evening mix Other account Already added")).to_be_disabled()


@pytest.mark.parametrize("width", [1280, 390, 320])
def test_long_song_title_and_artwork_survive_creation(song_app, page, width, tmp_path):
    title = "Checker Wrecker (feat. Jungle Boogie & Big Tony)"
    # Exercise both real image loading and a failed image's fallback.
    thumbnail = "/missing-cover.jpg" if width == 320 else "/static/road-trip-hero.png"
    track = song_app["store"].upsert_track("checker-wrecker", title, "Lettuce", "Elevate", 300,
                                          thumbnail=thumbnail)
    song_app["store"].set_playlist_tracks(song_app["source"], [track])
    page.set_viewport_size({"width": width, "height": 740})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    trigger = page.get_by_role("button", name=f"More actions for {title}")
    trigger.click()
    page.get_by_role("menuitem", name="Create playlist from song").click()
    dialog = page.get_by_role("dialog", name="Create playlist", exact=True)
    expect(dialog.get_by_label("Playlist name")).to_have_value(title)
    if width == 320:
        expect(dialog.locator(".song-dialog-art img")).to_have_count(0)
    else:
        expect(dialog.locator(".song-dialog-art img")).to_be_visible()
        page.wait_for_function("document.querySelector('.song-dialog-art img')?.naturalWidth > 0")
    page.screenshot(path=tmp_path / f"long-song-create-{width}.png", animations="disabled")
    dialog.get_by_role("button", name="Create playlist", exact=True).click()
    expect(dialog.get_by_text("Playlist created.", exact=True)).to_be_visible()
    expect(dialog.get_by_role("heading", name=title, exact=True)).to_be_visible()
    expect(dialog.get_by_role("link", name="Open playlist")).to_be_focused()
    assert dialog.get_by_text(title, exact=True).filter(visible=True).count() == 1
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    for _ in range(4):
        page.keyboard.press("Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    expect(dialog.get_by_role("link", name="Open playlist")).to_be_in_viewport()
    page.screenshot(path=tmp_path / f"long-song-created-{width}.png", animations="disabled")
    page.keyboard.press("Escape")
    expect(trigger).to_be_focused()
    saved = next(p for p in song_app["store"].get_playlists() if p.title == title)
    assert saved.thumbnail == thumbnail
    page.goto(f"{song_app['base']}/playlists")
    cover = page.locator(f'#new-playlists tr[data-playlist-id="{saved.id}"] img.pl-thumb')
    if width == 320:
        expect(cover).to_have_count(0)
    else:
        expect(cover).to_be_visible()
        expect(cover).to_have_attribute("src", thumbnail)
        page.wait_for_function("selector => document.querySelector(selector)?.naturalWidth > 0",
                               arg=f'#new-playlists tr[data-playlist-id="{saved.id}"] img.pl-thumb')


def test_partial_creation_keeps_warning_and_playlist_link(song_app, page, monkeypatch, tmp_path):
    def fail(*args):
        raise RuntimeError("Song unavailable")
    monkeypatch.setattr(song_app["main"], "add_playlist_items", fail)
    page.set_viewport_size({"width": 390, "height": 740})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role("button", name="More actions for Owned Song").click()
    page.get_by_role("menuitem", name="Create playlist from song").click()
    dialog = page.get_by_role("dialog", name="Create playlist", exact=True)
    dialog.get_by_role("button", name="Create playlist", exact=True).click()
    expect(dialog.get_by_role("status")).to_have_text("Playlist created, but YouTube couldn't add this song.")
    expect(dialog.get_by_role("heading", name="Owned Song", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Create playlist", exact=True)).to_be_hidden()
    expect(dialog.get_by_role("link", name="Open playlist")).to_be_focused()
    page.screenshot(path=tmp_path / "song-partially-created-390.png", animations="disabled")
    dialog.get_by_role("link", name="Open playlist").click()
    expect(page.get_by_role("heading", name="Owned Song", exact=True)).to_be_visible()
    assert len(song_app["main"].created) == 1


def test_keyboard_reuses_one_menu_after_row_replacement(song_app, page):
    store, source = song_app["store"], song_app["source"]
    second = store.upsert_track("second", "Second Song", "Someone", "", 200)
    store.set_playlist_tracks(source, store.get_playlist_track_ids(source) + [second])
    page.goto(f"{song_app['base']}/playlist/{source}")
    first = page.get_by_role("button", name="More actions for Owned Song")
    first.focus()
    first.press("Enter")
    menu = page.get_by_role("menu", name="Song actions")
    expect(menu.get_by_role("menuitem", name="Play on YouTube Music")).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.get_by_role("menuitem", name="Songs like this")).to_be_focused()
    page.keyboard.press("Escape")
    expect(first).to_be_focused()
    expect(first).to_have_attribute("aria-expanded", "false")
    first.click()
    expect(menu).to_be_visible()
    first.click()
    expect(menu).not_to_be_visible()
    row = page.get_by_role("row").filter(has_text="Second Song")
    row.locator(".ydisplay").click()
    row.locator(".yinput").fill("2003")
    row.locator(".yinput").press("Enter")
    expect(row.get_by_text("2003", exact=True)).to_be_visible()
    row.get_by_role("button", name="More actions for Second Song").click()
    expect(page.locator("#song-action-menu")).to_have_count(1)
    expect(menu.get_by_role("menuitem", name="More like this", exact=False)).to_have_count(0)
    menu.get_by_role("menuitem", name="Create playlist from song").click()
    expect(page.get_by_label("Playlist name")).to_have_value("Second Song")


def test_suggestion_feedback_uses_shared_menu(song_app, page):
    store = song_app["store"]
    owned = store.get_playlist_track_ids(song_app["source"])[0]
    bonus = store.upsert_track("bonus", "Bonus Song", "Artist & Friends", "", 200)
    for tid in [owned, bonus]:
        store.set_track_genre(tid, "Techno")
    store.set_playlist_tracks(song_app["destination"], [owned, bonus])
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    tile = page.locator(".tile-suggest").filter(has_text="Bonus Song")
    tile.get_by_role("button", name="More actions for Bonus Song").click()
    expect(page.locator("#song-action-menu")).to_have_count(1)
    with page.expect_response("**/recs/feedback") as response:
        page.get_by_role("menuitem", name="Wrong era").click()
    assert response.value.ok
    assert "reason=era" in response.value.request.post_data
    assert f"scope={song_app['source']}" in response.value.request.post_data
    expect(tile).to_have_count(0)


def test_now_playing_uses_shared_menu(song_app, page):
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {**SONG, "paused": False, "likeStatus": "INDIFFERENT"},
        "sensor_health": {"healthy": True}, "radio": False,
    }))
    page.goto(song_app["base"] + "/playlists")
    page.locator(".global-now-playing").get_by_role("button", name=f"More actions for {SONG['title']}").click()
    page.get_by_role("menuitem", name="Create playlist from song").click()
    expect(page.get_by_label("Playlist name")).to_have_value(SONG["title"])


def test_adding_suggestion_to_current_playlist_refreshes_when_done(song_app, page):
    store = song_app["store"]
    owned = store.get_playlist_track_ids(song_app["source"])[0]
    bonus = store.upsert_track("bonus", "Bonus Song", "Artist & Friends", "", 200)
    for tid in [owned, bonus]:
        store.set_track_genre(tid, "Techno")
    store.set_playlist_tracks(song_app["destination"], [owned, bonus])
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.locator(".tile-suggest").get_by_role("button", name="More actions for Bonus Song").click()
    page.get_by_role("menuitem", name="Add song to playlist", exact=True).click()
    dialog = page.get_by_role("dialog", name="Add to playlist", exact=True)
    dialog.get_by_role("radio", name="Source mix Main", exact=True).check()
    dialog.get_by_role("button", name="Add song", exact=True).click()
    expect(dialog.get_by_text("Song added.", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Done").click()
    expect(page.get_by_role("row").filter(has_text="Bonus Song")).to_be_visible()
    assert song_app["main"].added == [("SOURCE", ["bonus"])]


@pytest.mark.parametrize("surface", ["artist", "charts", "saved_album"])
def test_library_views_share_song_actions(song_app, page, surface):
    store = song_app["store"]
    store.upsert_track("owned-song", "Owned Song", "Artist & Friends", "Album", 180,
                       album_browse_id="MPRE_owned")
    store.add_saved_album({"browse": "MPRE_owned", "title": "Album", "artist": "Artist & Friends",
                           "year": "2000", "thumbnail": ""})
    store.add_history_snapshot(song_app["main_id"], 50, [store.identity_key_for_video("owned-song")])
    path = {"artist": "/artist?name=Artist%20%26%20Friends", "charts": "/charts",
            "saved_album": "/album?browse=MPRE_owned"}[surface]
    page.goto(song_app["base"] + path)
    page.get_by_role("button", name="More actions for Owned Song").click()
    expect(page.locator("#song-action-menu")).to_have_count(1)
    expect(page.get_by_role("menuitem", name="Remove from playlist")).to_have_count(0)
    page.get_by_role("menuitem", name="Create playlist from song").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Playlist name")).to_have_value("Owned Song")
    expect(dialog.get_by_text("Artist & Friends", exact=True)).to_be_visible()


def test_generated_playlist_keeps_contextual_mood_actions(song_app, page):
    song_app["store"].set_playlist_group("SOURCE", "Generated")
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    row = page.get_by_role("row").filter(has_text="Owned Song")
    row.get_by_role("button", name="More actions for Owned Song").click()
    with page.expect_response("**/recs/mood") as response:
        page.get_by_role("menuitem", name="🔥 More like this").click()
    assert response.value.ok
    expect(row.locator(".mood-flag")).to_have_text("🔥")
    expect(row.locator(".mood-flag")).to_be_visible()


def test_mix_track_menu_stays_above_card_and_opens_playlist_dialog(live_app, store, page, tmp_path):
    from tests.test_home_other_genres import seed_genres
    seed_genres(store)
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(live_app + "/")
    card = page.locator("#gen-wheelhouse")
    card.locator(".gen-poster").click()
    track = card.locator(".gen-row").first
    title = track.get_attribute("data-title")
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 850})
        track.get_by_role("button", name=f"More actions for {title}").click()
        menu = page.get_by_role("menu", name="Song actions")
        expect(menu).to_be_visible()
        bounds = menu.bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
        page.screenshot(path=tmp_path / f"mix-song-menu-{width}.png")
        menu.get_by_role("menuitem", name="Create playlist from song").click()
        expect(page.get_by_label("Playlist name")).to_have_value(title)
        page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
