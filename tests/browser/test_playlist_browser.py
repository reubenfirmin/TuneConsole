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
import json

import pytest
import uvicorn
from playwright.sync_api import expect
from websockets.sync.client import connect

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN
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
        catalog={"valt": {"videoId": "valt", "setVideoId": "svalt", "title": "Song A (Live)",
                           "artists": [{"name": "Artist X"}], "duration_seconds": 210}},
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
    yield {"base": f"http://127.0.0.1:{port}", "pid": pid, "store": s, "client": client}
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


def test_conflict_provider_opens_lookup_without_changing_choice(live_playlist_app, page):
    base, pid, store = (live_playlist_app[key] for key in ("base", "pid", "store"))
    tid = store.playlist_tracks_detail(pid)[0]["track_id"]
    store.set_track_enrichment(tid, None, "1952")
    store.upsert_conflict(tid, "year", [{"provider": "musicbrainz", "value": "1952"},
                                         {"provider": "discogs", "value": "2001"}])
    page.context.route("https://www.discogs.com/**", lambda route: route.fulfill(body="Provider results"))
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("button", name="Resolve enrichment disagreements").click()
    modal = page.locator("#conflicts-modal")
    original, alternative = modal.get_by_role("radio", name="1952"), modal.get_by_role("radio", name="2001")
    expect(original).to_be_checked()

    with page.expect_popup() as opened:
        modal.get_by_role("link", name="discogs").click()
    popup = opened.value
    popup.wait_for_load_state()
    assert "q=Artist+X+Song+A" in popup.url
    popup.close()
    expect(original).to_be_checked()
    expect(modal.get_by_role("heading", name="Resolve enrichment disagreements")).to_be_visible()

    alternative.check()
    modal.get_by_role("button", name="Save choices").click()
    expect(modal.get_by_text("Nothing left to resolve.", exact=False)).to_be_visible()
    assert store.playlist_tracks_detail(pid)[0]["year"] == "2001"


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


@pytest.mark.parametrize("initial_status", ["INDIFFERENT", "LIKE"])
def test_playing_track_table_heart_survives_status_polls(live_playlist_app, page, initial_status):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    snapshot = {"type": "now-heartbeat", "title": "Song A", "artist": "Artist X",
                "videoId": "v0", "likeStatus": initial_status, "paused": False}
    desired_status = "LIKE" if initial_status == "INDIFFERENT" else "INDIFFERENT"
    pressed = "true" if desired_status == "LIKE" else "false"
    with connect(base.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps(snapshot))
        page.goto(f"{base}/playlist/{pid}")
        widget = page.locator(".global-now-playing")
        heart = page.get_by_role("row").filter(has_text="Song A").locator(".like-btn")
        expect(widget).to_be_visible()
        expect(heart).to_have_attribute("aria-pressed", "false" if pressed == "true" else "true")

        with page.expect_response(lambda r: r.url.endswith(("/now-playing/rate", "/track/like")),
                                  timeout=5000):
            heart.click()
        expect(heart).to_have_attribute("aria-pressed", pressed)
        # A poll can arrive before the player applies the click. It must not undo the user's choice.
        page.evaluate("""() => {
          window.ratingPolled = false;
          window.addEventListener('track-like-changed', () => { window.ratingPolled = true; }, {once: true});
        }""")
        with page.expect_response(lambda r: r.url.endswith("/bridge/status"), timeout=8000) as polled:
            pass
        assert polled.value.json()["now_playing"]["likeStatus"] == initial_status
        page.wait_for_function("window.ratingPolled")
        expect(heart).to_have_attribute("aria-pressed", pressed)
        if pressed == "true":
            expect(widget.locator(".hs-heart")).to_have_class(re.compile(r"\bon\b"))
        else:
            expect(widget.locator(".hs-heart")).not_to_have_class(re.compile(r"\bon\b"))

        frame = json.loads(extension.recv(timeout=5))
        while frame == {"ping": 1}:
            frame = json.loads(extension.recv(timeout=5))
        assert frame == {"type": "rate", "action": "like"}

        extension.send(json.dumps({**snapshot, "likeStatus": desired_status}))
        with page.expect_response(lambda r: r.url.endswith("/bridge/status")
                                  and r.json().get("now_playing", {}).get("likeStatus") == desired_status,
                                  timeout=8000):
            pass
        expect(heart).to_have_attribute("aria-pressed", pressed)
        assert live_playlist_app["client"].rated == []  # Only the player control writes this rating.


def test_other_track_table_heart_keeps_using_liked_music(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    with connect(base.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps({"type": "now-heartbeat", "title": "Song A", "artist": "Artist X",
                                   "videoId": "v0", "likeStatus": "INDIFFERENT", "paused": False}))
        page.goto(f"{base}/playlist/{pid}")
        expect(page.locator(".global-now-playing")).to_be_visible()
        heart = page.get_by_role("row").filter(has_text="Song B").locator(".like-btn")
        with page.expect_response(lambda r: r.url.endswith("/track/like")):
            heart.click()
        expect(heart).to_have_attribute("aria-pressed", "true")
        assert live_playlist_app["client"].rated == [("v1", "LIKE")]
        with page.expect_response(lambda r: r.url.endswith("/bridge/status"), timeout=8000):
            pass
        expect(heart).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".global-now-playing .hs-heart")).not_to_have_class(re.compile(r"\bon\b"))


@pytest.mark.parametrize("failure", ["rejected", "network"])
def test_playing_track_table_heart_reverts_on_failed_rating(live_playlist_app, page, failure):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    if failure == "rejected":
        page.route("**/now-playing/rate", lambda route: route.fulfill(json={"ok": False}))
    else:
        page.route("**/now-playing/rate", lambda route: route.abort())
    with connect(base.replace("http://", "ws://") + "/bridge/ws",
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        extension.send(json.dumps({"type": "now-heartbeat", "title": "Song A", "artist": "Artist X",
                                   "videoId": "v0", "likeStatus": "INDIFFERENT", "paused": False}))
        page.goto(f"{base}/playlist/{pid}")
        expect(page.locator(".global-now-playing")).to_be_visible()
        heart = page.get_by_role("row").filter(has_text="Song A").locator(".like-btn")
        heart.click()
        expect(heart).to_have_attribute("aria-pressed", "false")
        expect(page.locator(".global-now-playing .hs-heart")).not_to_have_class(re.compile(r"\bon\b"))
        assert live_playlist_app["client"].rated == []


def test_remove_track_drops_row(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song B")
    row.get_by_title("More actions").click()                        # ⋯ menu
    page.get_by_role("menuitem", name="Remove from playlist").click()  # opens confirm modal
    page.get_by_role("button", name="Remove", exact=True).click()   # confirm
    expect(page.get_by_role("row").filter(has_text="Song B")).to_have_count(0)
    expect(page.get_by_role("link", name="Song A", exact=True)).to_be_visible()


@pytest.mark.parametrize("indexing_lag", [False, True])
def test_find_and_add_alternate_version(live_playlist_app, page, indexing_lag):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    client = live_playlist_app["client"]
    if indexing_lag:
        client._catalog["valt"].pop("setVideoId")
    page.goto(f"{base}/playlist/{pid}")
    row = page.get_by_role("row").filter(has_text="Song A")
    row.get_by_title("More actions").click()
    page.get_by_role("menuitem", name="Find alternate versions").click()
    expect(page.get_by_text("Song A (Live)")).to_be_visible()        # htmx-rendered search results
    page.locator('#alt-results input[name="track"]').first.check()
    page.get_by_role("button", name="Add to playlist").click()
    # HX-Refresh reload -> the chosen alternate is now a row in the table
    expect(page.get_by_role("link", name="Song A (Live)", exact=True)).to_be_visible()
    retry = page.get_by_role("button", name="Retry placement")
    if indexing_lag:
        expect(page.get_by_role("status").filter(has_text="placement after the original song")).to_be_visible()
        assert [t["videoId"] for t in client._tracks["PL1"]] == ["v0", "v1", "valt"]
        client._catalog["valt"]["setVideoId"] = "svalt"
        retry.click()
    expect(retry).to_have_count(0)
    assert [t["videoId"] for t in client._tracks["PL1"]] == ["v0", "valt", "v1"]
    assert [t["video_id"] for t in live_playlist_app["store"].playlist_tracks_detail(pid)] == ["v0", "valt", "v1"]
    assert client.added == [("PL1", ["valt"])]


def test_enrich_updates_cells_live(live_playlist_app, page, monkeypatch):
    # Enrichment is one waterfall button now (the header's "Enrich" icon runs every enabled
    # provider in order); isolate MusicBrainz and stub its lookup.
    import yt_playlist.providers.musicbrainz as mb
    from tests.conftest import only_provider
    monkeypatch.setattr(mb, "enrich_full",
                        lambda title, artist, **kwargs: ("Electronic", "1998", None, []) if title == "Song B"
                        else (None, None, None, []))
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    only_provider(live_playlist_app["store"], "musicbrainz")
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("button", name="Enrich", exact=True).click()
    row = page.get_by_role("row").filter(has_text="Song B")
    expect(row.get_by_text("Electronic")).to_be_visible(timeout=5000)   # live SSE cell update
    expect(row.get_by_text("1998")).to_be_visible()
