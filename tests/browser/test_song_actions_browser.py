"""The same song menu works across views; playlist writes and modal focus stay predictable."""
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

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
    expect(page.get_by_role("link", name=SONG["title"], exact=True)).to_be_visible()
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
    track = song_app["store"].tracks.upsert_track("checker-wrecker", title, "Lettuce", "Elevate", 300,
                                          thumbnail=thumbnail)
    song_app["store"].playlists.set_playlist_tracks(song_app["source"], [track])
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
    saved = next(p for p in song_app["store"].playlists.get_playlists() if p.title == title)
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


@pytest.mark.parametrize('unknown', [False, True])
def test_partial_creation_keeps_warning_and_playlist_link(song_app, page, monkeypatch, tmp_path, unknown):
    def fail(*args):
        if unknown:
            raise RuntimeError("Song response lost")
        return {"status": "STATUS_FAILED"}
    monkeypatch.setattr(song_app["main"], "add_playlist_items", fail)
    page.set_viewport_size({"width": 390, "height": 740})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role("button", name="More actions for Owned Song").click()
    page.get_by_role("menuitem", name="Create playlist from song").click()
    dialog = page.get_by_role("dialog", name="Create playlist", exact=True)
    dialog.get_by_role("button", name="Create playlist", exact=True).click()
    expect(dialog.get_by_role("status")).to_have_text(
        "Playlist created, but YouTube has not confirmed this song." if unknown else
        "Playlist created, but YouTube couldn't add this song.")
    expect(dialog.get_by_role("heading", name="Owned Song", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Create playlist", exact=True)).to_be_hidden()
    link = dialog.get_by_role("link", name="Review playlist" if unknown else "Open playlist")
    expect(link).to_be_focused()
    if unknown:
        expect(dialog.get_by_role('link', name='Action Log and recovery')).to_be_visible()
    dialog.locator('form').evaluate('form => form.requestSubmit()')
    page.screenshot(path=tmp_path / "song-partially-created-390.png", animations="disabled")
    link.press('Enter')
    expect(page.get_by_role("heading", name="Owned Song", exact=True)).to_be_visible()
    assert len(song_app["main"].created) == 1


@pytest.mark.parametrize("control", ["cover", "menu"])
def test_playlist_play_starts_selected_track_in_playlist(song_app, page, control, tmp_path):
    store, source = song_app["store"], song_app["source"]
    title = "Revenge Of The Wankers (Kora Remix) (feat. Scarlett De La Torre)"
    track = store.tracks.upsert_track("second", title, "Oceanvs Orientalis", "Album", 270,
                                     thumbnail="/static/road-trip-hero.png")
    store.tracks.set_track_genre(track, "Turkish")
    store.playlists.set_playlist_tracks(source, store.playlists.get_playlist_track_ids(source) + [track])
    page.set_viewport_size({"width": 1280, "height": 850})
    page.route("**/play", lambda route: route.fulfill(json={"ok": True}))
    page.goto(f"{song_app['base']}/playlist/{source}")
    row = page.locator('tr.srow[data-vid="second"]')

    def play():
        with page.expect_request(lambda request: request.url.endswith("/play") and request.method == "POST") as request:
            if control == "cover":
                row.hover()
                row.locator(".pl-play").click()
            else:
                row.get_by_role("button", name=f"More actions for {title}").click()
                page.get_by_role("menuitem", name="Play on YouTube Music").click()
        url = urlparse(request.value.post_data_json["url"])
        assert url.netloc == "music.youtube.com" and url.path == "/watch"
        assert parse_qs(url.query) == {"v": ["second"], "list": ["SOURCE"]}

    play()
    page.screenshot(path=tmp_path / f"playlist-top-aligned-{control}.png", animations="disabled")
    # Both manual updates and enrichment replace a complete row with this fragment.
    response = page.request.post(f"{song_app['base']}/playlist/{source}/track-year",
                                 form={"video_id": "second", "year": "2019"})
    assert response.ok
    page.evaluate("html => enrichPanel(1, false).applyRow({video_id: 'second', row_html: html})",
                  response.text())
    expect(row.get_by_text("2019", exact=True)).to_be_visible()
    play()


@pytest.mark.parametrize("surface", ["playlist", "album", "radio"])
def test_track_alignment_centers_single_lines_and_anchors_wrapped_lines(song_app, page, surface, monkeypatch, tmp_path):
    store, source = song_app["store"], song_app["source"]
    tracks = []
    for video_id, title in [("single-line", "Song"), ("wrapped-line",
            "Revenge Of The Wankers (Kora Remix) (feat. Scarlett De La Torre)")]:
        track = store.tracks.upsert_track(video_id, title, "Artist", "Album", 270,
                                         album_browse_id="MPRE_alignment", thumbnail="/static/road-trip-hero.png")
        store.tracks.set_track_enrichment(track, "House", "2019")
        tracks.append(track)
    store.playlists.set_playlist_tracks(source, tracks)
    store.collection.add_saved_album({"browse": "MPRE_alignment", "title": "Album", "artist": "Artist"})
    if surface == "radio":
        monkeypatch.setattr(song_app["app"].state.ctx.radio, "status", lambda: {
            "local_id": source, "playlist_id": "SOURCE", "history_count": 2, "title": "Radio Session"})
    path = "/album?browse=MPRE_alignment" if surface == "album" else f"/playlist/{source}"
    page.goto(song_app["base"] + path)
    if surface == "radio":
        page.add_style_tag(url=song_app["base"] + "/static/radio.css")
        page.evaluate("() => htmx.ajax('GET', '/radio/playlist', {target: '.library-table-card', swap: 'outerHTML'})")
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 850})
        for video_id in ("single-line", "wrapped-line"):
            row = page.locator(f'tr.srow[data-vid="{video_id}"]')
            expect(row).to_be_visible()
            offsets = row.evaluate("""row => {
                const cover = row.querySelector('.pl-cover').getBoundingClientRect();
                const anchor = cover.y + cover.height / 2;
                const offsets = [];
                for (const cell of row.cells) {
                    if (!cell.getClientRects().length) continue;
                    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT,
                        {acceptNode: node => node.textContent.trim() && !node.parentElement.closest('.pl-play, .rowmenu, .liked-col')
                            ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT});
                    const text = walker.nextNode();
                    if (!text) continue;
                    const range = document.createRange();
                    range.selectNodeContents(text);
                    const rect = range.getClientRects()[0];
                    offsets.push({cell: cell.cellIndex, offset: rect.y + rect.height / 2 - anchor});
                }
                for (const selector of ['.like-btn', '.song-menu-trigger']) {
                    const rect = row.querySelector(selector)?.getBoundingClientRect();
                    if (rect?.height) offsets.push({cell: selector, offset: rect.y + rect.height / 2 - anchor});
                }
                return offsets;
            }""")
            assert all(abs(item["offset"]) <= 3 for item in offsets), (surface, width, video_id, offsets)
        page.locator(".track-table").screenshot(path=tmp_path / f"track-alignment-{surface}-{width}.png")


def test_keyboard_reuses_one_menu_after_metadata_save(song_app, page):
    store, source = song_app["store"], song_app["source"]
    second = store.tracks.upsert_track("second", "Second Song", "Someone", "", 200)
    store.playlists.set_playlist_tracks(source, store.playlists.get_playlist_track_ids(source) + [second])
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
    row.get_by_role("button", name="More actions for Second Song").click()
    menu.get_by_role("menuitem", name="Edit Metadata").click()
    dialog = page.get_by_role("dialog", name="Edit Metadata")
    dialog.get_by_label("Year", exact=True).fill("2003")
    dialog.get_by_role("button", name="Save changes").click()
    expect(row.get_by_text("2003", exact=True)).to_be_visible()
    row.get_by_role("button", name="More actions for Second Song").click()
    expect(page.locator("#song-action-menu")).to_have_count(1)
    expect(menu.get_by_role("menuitem", name="More like this", exact=False)).to_have_count(0)
    menu.get_by_role("menuitem", name="Create playlist from song").click()
    expect(page.get_by_label("Playlist name")).to_have_value("Second Song")


def test_suggestion_feedback_uses_shared_menu(song_app, page):
    store = song_app["store"]
    owned = store.playlists.get_playlist_track_ids(song_app["source"])[0]
    bonus = store.tracks.upsert_track("bonus", "Bonus Song", "Artist & Friends", "", 200)
    for tid in [owned, bonus]:
        store.tracks.set_track_genre(tid, "Techno")
    store.playlists.set_playlist_tracks(song_app["destination"], [owned, bonus])
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


def test_now_playing_has_playback_controls_without_track_menu(song_app, page):
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {**SONG, "paused": False, "likeStatus": "INDIFFERENT"},
        "sensor_health": {"healthy": True}, "radio": False,
    }))
    page.goto(song_app["base"] + "/playlists")
    player = page.locator(".global-now-playing")
    expect(player).to_be_visible()
    expect(player.get_by_role("button", name="More actions", exact=False)).to_have_count(0)
    expect(player.get_by_role("button", name="Next / skip")).to_be_visible()


def test_adding_suggestion_to_current_playlist_refreshes_when_done(song_app, page):
    store = song_app["store"]
    owned = store.playlists.get_playlist_track_ids(song_app["source"])[0]
    bonus = store.tracks.upsert_track("bonus", "Bonus Song", "Artist & Friends", "", 200)
    for tid in [owned, bonus]:
        store.tracks.set_track_genre(tid, "Techno")
    store.playlists.set_playlist_tracks(song_app["destination"], [owned, bonus])
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
    store.tracks.upsert_track("owned-song", "Owned Song", "Artist & Friends", "Album", 180,
                       album_browse_id="MPRE_owned")
    store.collection.add_saved_album({"browse": "MPRE_owned", "title": "Album", "artist": "Artist & Friends",
                           "year": "2000", "thumbnail": ""})
    store.history.record_play_event(song_app["main_id"],
        store.tracks.identity_key_for_video("owned-song"), "owned-song", 50, source_timezone="UTC")
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
    song_app["store"].playlists.set_playlist_group("SOURCE", "Generated")
    song_app['store'].playlists.mark_generated_playlist('SOURCE')
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    row = page.get_by_role("row").filter(has_text="Owned Song")
    trigger = row.get_by_role("button", name="More actions for Owned Song")
    initial_width = trigger.bounding_box()["width"]
    trigger.click()
    with page.expect_response("**/recs/mood") as response:
        page.get_by_role("menuitem", name="🔥 More like this").click()
    assert response.value.ok
    expect(trigger.locator(".mood-flag-flame")).to_be_visible()
    expect(trigger).to_have_attribute("title", "More actions · More like this is active")
    assert trigger.bounding_box()["width"] == initial_width
    page.reload()
    expect(trigger.locator(".mood-flag-flame")).to_be_visible()
    trigger.click()
    with page.expect_response("**/recs/mood"):
        page.get_by_role("menuitem", name="🙅 Less like this").click()
    expect(trigger.locator(".mood-flag-less")).to_be_visible()
    expect(trigger.locator(".mood-flag-flame")).not_to_be_visible()
    expect(trigger).to_have_attribute("title", "More actions · Less like this is active")
    assert trigger.bounding_box()["width"] == initial_width


def test_mix_track_menu_stays_above_card_and_opens_playlist_dialog(live_app, store, page, tmp_path):
    from tests.test_home_other_genres import seed_genres
    from tests.test_mode_cards_route import prepare_home
    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    prepare_home(store, 1)
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


@pytest.mark.parametrize('surface', ['playlist', 'album'])
@pytest.mark.parametrize('width', [1280, 390])
def test_metadata_dialog_edits_all_fields_and_restores_originals(song_app, page, surface, width, tmp_path):
    store = song_app['store']
    tid = store.tracks.upsert_track('owned-song', 'Owned Song', 'Artist & Friends', 'Album', 180,
                                   album_browse_id='MPRE_owned')
    store.collection.add_saved_album({'browse': 'MPRE_owned', 'title': 'Album', 'artist': 'Artist & Friends',
                                      'year': '2000', 'thumbnail': ''})
    store.tracks.set_track_enrichment(tid, 'Electronic', '1995')
    store.enrichment_state.log_enrichment(tid, 'run', 'discogs', 'genre', 'Art Pop', now=1)
    path = f"/playlist/{song_app['source']}" if surface == 'playlist' else '/album?browse=MPRE_owned'
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 850})
    page.goto(song_app['base'] + path)
    row = page.locator('tr.srow[data-vid="owned-song"]')
    expect(row.locator('input[type="text"], .editpen, .resetpen, .genre-info')).to_have_count(0)
    trigger = row.get_by_role('button', name='More actions for Owned Song')
    trigger.click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Title', exact=True)).to_be_focused()
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Owned Song')
    expect(dialog.get_by_label('Artist', exact=True)).to_have_value('Artist & Friends')
    expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Electronic')
    expect(dialog.get_by_label('Year', exact=True)).to_have_value('1995')
    dialog.get_by_label('Title', exact=True).fill('Unsaved title')
    page.keyboard.press('Escape')
    expect(trigger).to_be_focused()
    assert store.tracks.metadata_for_video('owned-song')['title'] == 'Owned Song'
    trigger.click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Owned Song')
    dialog.get_by_label('Title', exact=True).fill('Fixed title')
    dialog.get_by_label('Artist', exact=True).fill('Fixed artist')
    dialog.get_by_role('button', name='Art Pop discogs').click()
    dialog.get_by_label('Year', exact=True).fill('1998')
    assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth')
    for _ in range(12):
        page.keyboard.press('Tab')
        assert dialog.evaluate('el => el.contains(document.activeElement)')
    page.screenshot(path=tmp_path / f'metadata-{surface}-{width}.png', animations='disabled')
    dialog.get_by_role('button', name='Save changes').click()
    expect(row.locator('.ptitle')).to_have_text('Fixed title')
    expect(row.locator('.alink').first).to_have_text('Fixed artist')
    expect(row.locator('.gtag')).to_have_text('Art Pop')
    expect(row.locator('.ydisplay')).to_have_text('1998')
    expect(row).to_have_attribute('data-genre', 'art pop')
    expect(row).to_have_attribute('data-year', '1998')
    row.get_by_role('button', name='More actions for Fixed title').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog.get_by_role('button', name='Restore original title').click()
    dialog.get_by_role('button', name='Restore original artist').click()
    dialog.get_by_label('Genre', exact=True).fill('')
    dialog.get_by_label('Year', exact=True).fill('')
    dialog.get_by_role('button', name='Save changes').click()
    expect(row.locator('.ptitle')).to_have_text('Owned Song', use_inner_text=True)
    expect(row.locator('.gtag')).to_have_count(0)
    expect(row.locator('.ydisplay')).to_have_text('—')
    assert not errors


def test_metadata_delayed_focus_does_not_interrupt_artist_editing(song_app, page):
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role("button", name="More actions for Owned Song").click()
    page.get_by_role("menuitem", name="Edit Metadata").click()
    dialog = page.get_by_role("dialog", name="Edit Metadata")
    expect(dialog.get_by_role("button", name="Save changes")).to_be_enabled()
    expect(dialog.get_by_label("Title", exact=True)).to_be_focused()
    # A queued autofocus from the load must yield to the user's next field.
    dialog.evaluate("""async el => {
        const state = Alpine.$data(el);
        state.focusDialog('metadataTitle');
        await state.$nextTick();
        el.querySelector('input[x-model="metadata.artist"]').focus();
        await new Promise(requestAnimationFrame);
    }""")
    expect(dialog.get_by_label("Artist", exact=True)).to_be_focused()
    dialog.get_by_label("Artist", exact=True).fill("Corrected artist")
    expect(dialog.get_by_label("Title", exact=True)).to_have_value("Owned Song")


def test_metadata_save_failure_preserves_edits_for_retry(song_app, page):
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    title = dialog.get_by_label('Title', exact=True)
    expect(title).to_have_value('Owned Song')
    title.fill('Retry this title')
    page.route('**/songs/metadata', lambda route: route.fulfill(status=503, json={'detail': 'Try again later.'}))
    dialog.get_by_role('button', name='Save changes').click()
    expect(dialog.get_by_role('alert')).to_have_text('Try again later.')
    expect(title).to_have_value('Retry this title')
    assert song_app['store'].tracks.metadata_for_video('owned-song')['title'] == 'Owned Song'
    page.unroute('**/songs/metadata')
    dialog.get_by_role('button', name='Save changes').click()
    expect(page.locator('tr.srow .ptitle')).to_have_text('Retry this title')


def test_genre_lookup_keeps_unsaved_metadata(song_app, page, monkeypatch):
    def lookup(store, tracks, _config, _progress):
        store.enrichment.apply_provider_fields(tracks[0]['id'], 'discogs', {'genre': 'Ambient', 'year': '2022'})
        store.enrichment_state.log_enrichment(tracks[0]['id'], 'lookup', 'discogs', 'genre', 'Ambient', now=2)

    monkeypatch.setattr('yt_playlist.web.routes.enrich.waterfall.run_waterfall', lookup)
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    title = dialog.get_by_label('Title', exact=True)
    expect(title).to_have_value('Owned Song')
    title.fill('Still editing')
    dialog.get_by_role('button', name='Look up genres').click()
    suggestion = dialog.get_by_role('button', name='Ambient discogs')
    expect(suggestion).to_be_visible()
    expect(title).to_have_value('Still editing')
    suggestion.click()
    expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Ambient')
    with page.expect_response('**/songs/metadata') as response:
        dialog.get_by_role('button', name='Save changes').click()
    assert set(response.value.request.post_data_json) == {'song', 'title', 'genre'}
    expect(page.locator('tr.srow .ptitle')).to_have_text('Still editing')
    expect(page.locator('tr.srow .ydisplay')).to_have_text('2022')
    track = song_app['store'].tracks.metadata_for_video('owned-song')
    assert track['title'] == 'Still editing' and track['genre'] == 'Ambient' and track['year'] == '2022'


@pytest.mark.parametrize('succeeds', [True, False])
def test_metadata_save_closes_immediately_and_keeps_later_dialog(song_app, page, succeeds):
    store = song_app['store']
    second = store.tracks.upsert_track('second', 'Second Song', 'Someone', '', 200)
    store.playlists.set_playlist_tracks(song_app['source'],
                                       store.playlists.get_playlist_track_ids(song_app['source']) + [second])
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    # A reload would discard both this marker and the second dialog's unsaved fields.
    page.evaluate('window.metadataPageMarker = true')
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Owned Song')
    dialog.get_by_label('Title', exact=True).fill('Saved in background')
    pending = []
    page.route('**/songs/metadata', lambda route: pending.append(route))
    with page.expect_request(lambda request: request.url.endswith('/songs/metadata') and request.method == 'POST'):
        dialog.get_by_role('button', name='Save changes').click()
    expect(dialog).not_to_be_visible()
    expect(page.locator('tr.srow .ptitle').first).to_have_text('Owned Song')
    page.get_by_role('button', name='More actions for Second Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Second Song')
    dialog.get_by_label('Artist', exact=True).fill('Still editing this artist')
    if succeeds:
        pending[0].continue_()
        expect(page.locator('tr.srow .ptitle').first).to_have_text('Saved in background')
    else:
        pending[0].fulfill(status=503, json={'detail': 'Try again later.'})
        expect(page.locator('#toasts').get_by_role('alert')).to_contain_text('Try again later.')
        assert store.tracks.metadata_for_video('owned-song')['title'] == 'Owned Song'
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Second Song')
    expect(dialog.get_by_label('Artist', exact=True)).to_have_value('Still editing this artist')
    assert page.evaluate('window.metadataPageMarker')
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    if not succeeds:
        page.unroute('**/songs/metadata')
        page.get_by_role('button', name='Retry edit').click()
        expect(dialog.get_by_label('Title', exact=True)).to_have_value('Saved in background')
        dialog.get_by_role('button', name='Save changes').click()
        expect(page.locator('tr.srow .ptitle').first).to_have_text('Saved in background')
        expect(page.get_by_role('button', name='Retry edit')).to_have_count(0)


def test_metadata_save_updates_player_and_suggestion_without_reload(song_app, page):
    store = song_app['store']
    owned = store.playlists.get_playlist_track_ids(song_app['source'])[0]
    bonus = store.tracks.upsert_track('bonus', 'Bonus Song', 'Artist & Friends', 'Bonus Album', 200)
    for track in [owned, bonus]:
        store.tracks.set_track_genre(track, 'Techno')
    store.playlists.set_playlist_tracks(song_app['destination'], [owned, bonus])
    status = {'connected': True, 'now_playing': {
        'video_id': 'bonus', 'title': 'Bonus Song', 'artist': 'Artist & Friends', 'paused': False,
    }, 'sensor_health': {'healthy': True}, 'radio': False}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    tile = page.locator('.tile-suggest').filter(has=page.locator('.tile-play[href$="v=bonus"]'))
    tile.get_by_role('button', name='More actions for Bonus Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Bonus Song')
    dialog.get_by_label('Title', exact=True).fill('Fixed <title>')
    dialog.get_by_label('Artist', exact=True).fill('Fixed & artist')
    dialog.get_by_role('button', name='Save changes').click()
    expect(tile.locator('.tile-cap-title')).to_have_text('Fixed <title>')
    expect(tile.locator('.tile-cap-sub')).to_have_text('Fixed & artist · Bonus Album')
    assert tile.locator('input[name="track"]').evaluate('el => JSON.parse(el.value).title') == 'Fixed <title>'
    dock = page.locator('.global-now-playing')
    expect(dock.locator('.hs-now-title')).to_have_text('Fixed <title>')
    expect(dock.locator('.hs-now-artist')).to_have_text('Fixed & artist')
    # The extension still reports the provider's title; polling must keep the saved override.
    page.evaluate('status => window.dispatchEvent(new CustomEvent("bridge-status", {detail: status}))', status)
    expect(dock.locator('.hs-now-title')).to_have_text('Fixed <title>')
    tile.get_by_role('button', name='More actions for Fixed <title>').click()
    page.get_by_role('menuitem', name='Create playlist from song').click()
    expect(page.get_by_label('Playlist name')).to_have_value('Fixed <title>')
