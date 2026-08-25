"""Live-behavior tests for the playlist detail page (/playlist/{id}).

Own fixture (a playlist with a couple of tracks; the shared live_app seeds discover data
and is off-limits). Characterization first: lock the CURRENT Alpine behavior, then keep it
green after each Alpine->htmx conversion (rename, year/genre cells, enrich, remove, reorder,
alternates). Asserts on user-visible outcomes, not the transport.
"""
import socket
import threading
import time
import re

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_playlist_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("Main", "cred", None, True)
    pid = s.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    t0 = s.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)
    t1 = s.upsert_track("v1", "Song B", "Artist Y", "Alb", 200, 1)
    s.set_playlist_tracks(pid, [t0, t1])
    s.set_track_genre(t0, "Rock")            # one track starts with a genre, one blank
    client = FakeClient(
        tracks={"PL1": [{"videoId": "v0", "setVideoId": "sv0"},
                        {"videoId": "v1", "setVideoId": "sv1"}]},
        search_results=[  # alternate-version search (source is excluded by find_alternates)
            {"videoId": "v0", "title": "Song A", "artists": [{"name": "Artist X"}], "duration_seconds": 200},
            {"videoId": "valt", "title": "Song A (Live)", "artists": [{"name": "Artist X"}], "duration_seconds": 210}])
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "pid": pid, "store": s}
    server.should_exit = True
    thread.join(timeout=5)


def test_rename_playlist_updates_heading(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("heading", name="Mix").click()        # click-to-edit
    inp = page.locator("input.title-input")
    inp.fill("Renamed Mix")
    inp.press("Enter")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()
    # survives a reload (persisted, not just a DOM poke)
    page.goto(f"{base}/playlist/{pid}")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()


def test_set_year_click_to_edit(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song B")
    row.locator(".ydisplay").click()                       # click-to-edit
    row.locator(".yinput").fill("1999")
    row.locator(".yinput").press("Enter")
    expect(row.get_by_text("1999")).to_be_visible()        # cell shows the new year
    page.goto(f"{base}/playlist/{pid}")                    # persisted
    expect(page.get_by_role("row").filter(has_text="Song B").get_by_text("1999")).to_be_visible()


def test_genre_control_opens_dialog_and_accepts_custom_value(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song B")
    row.get_by_role("button", name="See or change genre for Song B").click()
    expect(page.get_by_role("heading", name="Genre for “Song B”")).to_be_visible()
    override = page.get_by_role("button", name="Override")
    apply = page.get_by_role("button", name="Apply genre")
    ob, ab = override.bounding_box(), apply.bounding_box()
    assert abs((ob["y"] + ob["height"] / 2) - (ab["y"] + ab["height"] / 2)) < 2
    override.click()
    suggestions = page.locator(".genre-suggest")
    expect(suggestions).to_be_visible()
    assert suggestions.bounding_box()["y"] + suggestions.bounding_box()["height"] <= \
        page.locator(".genre-picker .modal-actions").bounding_box()["y"]
    page.get_by_label("Custom genre").fill("Jazz Fusion")
    page.get_by_role("button", name="Apply genre").click()
    expect(page.get_by_role("heading", name="Mix")).to_be_visible()  # HX-Refresh completed
    row = page.get_by_role("row").filter(has_text="Song B")
    expect(row.get_by_text("Jazz Fusion", exact=True)).to_be_visible()


def test_genre_badge_itself_opens_candidate_dialog(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song A")
    row.get_by_text("Rock", exact=True).click()
    expect(page.get_by_role("heading", name="Genre for “Song A”")).to_be_visible()


def test_now_playing_heart_immediately_updates_matching_playlist_row(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True,
        "now_playing": {"title": "Song A", "artist": "Artist X", "thumbnail": "",
                        "likeStatus": "INDIFFERENT", "video_id": "v0", "paused": False},
        "sensor_health": {"healthy": True, "ytm_tabs": 1, "responding_tabs": 1,
                          "reinjected_tabs": 0, "error": ""},
        "radio": False, "radio_waiting": False, "radio_dual": False,
        "radio_fallback_reason": None, "radio_upcoming": [],
    }))
    page.route("**/now-playing/rate", lambda route: route.fulfill(json={"ok": True}))
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song A")
    expect(row.locator(".like-btn")).not_to_have_class(re.compile(r".*\bon\b.*"))
    page.locator(".global-now-playing .hs-heart").click()
    expect(row.locator(".like-btn")).to_have_class(re.compile(r".*\bon\b.*"))


def test_remove_track_drops_row(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song B")
    row.get_by_title("More actions").click()                        # ⋯ menu
    row.get_by_role("button", name="Remove from playlist").click()  # opens confirm modal
    page.get_by_role("button", name="Remove", exact=True).click()   # confirm
    expect(page.get_by_role("row").filter(has_text="Song B")).to_have_count(0)
    expect(page.get_by_role("link", name="Song A ↗")).to_be_visible()           # other row stays


def test_find_and_add_alternate_version(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song A")
    row.get_by_title("More actions").click()
    row.get_by_role("button", name="Find alternate versions").click()
    expect(page.get_by_text("Song A (Live)")).to_be_visible()        # htmx-rendered search results
    page.locator('#alt-results input[name="track"]').first.check()
    page.get_by_role("button", name="Add to playlist").click()
    # HX-Refresh reload -> the chosen alternate is now a row in the table
    expect(page.get_by_role("link", name="Song A (Live) ↗")).to_be_visible()


def test_enrich_updates_cells_live(live_playlist_app, page, monkeypatch):
    # Enrichment is one waterfall button now (the header's "Enrich" icon runs every enabled
    # provider in order); isolate MusicBrainz and stub its lookup.
    import yt_playlist.providers.musicbrainz as mb
    from tests.conftest import only_provider
    monkeypatch.setattr(mb, "enrich_full",
                        lambda title, artist: ("Electronic", "1998", None, []) if title == "Song B"
                        else (None, None, None, []))
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    only_provider(live_playlist_app["store"], "musicbrainz")
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("button", name="Enrich", exact=True).click()
    row = page.get_by_role("row").filter(has_text="Song B")
    expect(row.get_by_text("Electronic")).to_be_visible(timeout=5000)   # live SSE cell update
    expect(row.get_by_text("1998")).to_be_visible()
