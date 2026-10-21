"""Live enrichment rows retain working HTMX and Alpine controls after repeated updates."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("scope", ["playlist", "album"])
def test_replaced_rows_keep_genre_and_year_controls(live_app, store, page, scope):
    entry = store.upsert_track("live-row", "Live row song", "Artist", "Album", 200,
                               album_browse_id="BID")
    store.set_track_genre(entry, "Rock")
    playlist = store.conn.execute("SELECT id FROM playlists").fetchone()[0]
    store.set_playlist_tracks(playlist, [entry])
    store.replace_saved_albums([{"browse": "BID", "title": "Album", "artist": "Artist",
                                 "year": "2000", "type": "Album", "thumbnail": None}])
    page.goto(f"{live_app}/playlist/{playlist}" if scope == "playlist" else f"{live_app}/album?browse=BID")
    row = page.locator('tr.srow[data-vid="live-row"]')
    expect(row).to_be_visible()
    page.evaluate("""() => {
        window.rowLifecycle = {initialized: 0, destroyed: 0};
        const row = document.querySelector('tr.srow[data-vid="live-row"]');
        const template = row.cloneNode(true);
        template.setAttribute('x-data', `{init() { window.rowLifecycle.initialized++; },
            destroy() { window.rowLifecycle.destroyed++; }}`);
        window.liveRowHTML = template.outerHTML;
    }""")
    genre_requests = []
    page.on("request", lambda request: genre_requests.append(request.url)
            if request.url.endswith("/genre-candidates") and request.method == "GET" else None)
    for iteration in range(3):
        page.evaluate("""() => enrichPanel(1, false).applyRow({video_id: 'live-row',
            row_html: window.liveRowHTML})""")
        page.wait_for_function("n => window.rowLifecycle.initialized === n", arg=iteration + 1)
        assert page.evaluate("window.rowLifecycle.destroyed") == iteration
        row.get_by_role("button", name="See or change genre for Live row song").click()
        modal = page.locator("#genre-candidates-modal .genre-candidates")
        expect(modal).to_be_visible()
        assert len(genre_requests) == iteration + 1
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()
        row.locator(".ydisplay").click()
        expect(row.locator(".yinput")).to_be_visible()
        row.locator(".yinput").press("Escape")
        expect(row.locator(".yinput")).to_be_hidden()
