"""Live enrichment rows retain the shared metadata action after repeated updates."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("scope", ["playlist", "album"])
def test_replaced_rows_keep_metadata_menu(live_app, store, page, scope):
    entry = store.tracks.upsert_track("live-row", "Live row song", "Artist", "Album", 200,
                               album_browse_id="BID")
    store.tracks.set_track_genre(entry, "Rock")
    playlist = store.conn.execute("SELECT id FROM playlists").fetchone()[0]
    store.playlists.set_playlist_tracks(playlist, [entry])
    store.collection.replace_saved_albums([{"browse": "BID", "title": "Album", "artist": "Artist",
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
    metadata_requests = []
    page.on("request", lambda request: metadata_requests.append(request.url)
            if '/songs/metadata?' in request.url and request.method == "GET" else None)
    for iteration in range(3):
        page.evaluate("""() => enrichPanel(1, false).applyRow({video_id: 'live-row',
            row_html: window.liveRowHTML})""")
        page.wait_for_function("n => window.rowLifecycle.initialized === n", arg=iteration + 1)
        assert page.evaluate("window.rowLifecycle.destroyed") == iteration
        row.get_by_role("button", name="More actions for Live row song").click()
        page.get_by_role('menuitem', name='Edit Metadata').click()
        modal = page.get_by_role('dialog', name='Edit Metadata')
        expect(modal).to_be_visible()
        expect(modal.get_by_label('Genre', exact=True)).to_have_value('Rock')
        expect(modal.get_by_label('Year', exact=True)).to_be_visible()
        assert len(metadata_requests) == iteration + 1
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()
