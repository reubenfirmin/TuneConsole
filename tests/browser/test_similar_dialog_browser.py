"""#104/#106: scrolled playlist/album dialogs fit the viewport and keep actions reachable."""
import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.util.matching import identity_key
from tests.prepared_fixture_support import prepared_template


@pytest.fixture(scope='module')
def similar_catalog_template(_pristine_current_schema):
    def seed(store):
        store.collection.add_saved_album({
            'browse': 'MPRE_layout', 'title': 'Layout release', 'artist': 'Layout artist'})
        tids, vectors = [], []
        for i in range(40):
            title = f'Song {i:02d} ' + 'A long release title with additional details ' * 3 + 'x' * 80
            artist = f'Artist {i:02d} with a long artist credit'
            tids.append(store.tracks.upsert_track(f'layout{i}', title, artist, 'Layout release', 200,
                album_browse_id='MPRE_layout', thumbnail='/static/nag/intro.png'))
            vectors.append((identity_key(title, artist), np.array([1, i / 100], dtype=np.float32).tobytes()))
        store.rec_model.replace_rec_vectors(vectors)
        store.settings.set_setting('rec_dim', '2')
        return tids

    # Ingestion and vector publication are input to this layout consumer. Keep
    # all forty long rows and retained files, with a private restore per case.
    with prepared_template(_pristine_current_schema, seed) as template:
        yield template


@pytest.fixture
def similar_app(request, store, similar_catalog_template, monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import album, app_update, audio_preview, home, playlists, songs, suggest

    # Keep the real pages, menus and similarity endpoint; unrelated application
    # routers have full-app owners such as the TV navigation journey.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (album, app_update, audio_preview, home, playlists, songs, suggest)])
    similar_catalog_template.restore(store)
    return request.getfixturevalue('live_app')


@pytest.fixture(params=[(kind, width, artwork) for kind in ("playlist", "album")
                       for width in (1280,) for artwork in (False, True)],
                ids=lambda value: f"{value[0]}-{value[1]}-{'artwork' if value[2] else 'no-artwork'}")
def similar_dialog(similar_app, store, page, request, similar_catalog_template):
    kind, width, artwork = request.param
    playlist = store.playlists.get_playlists()[0]
    browse = "MPRE_layout"
    store.playlists.set_playlist_tracks(playlist.id, similar_catalog_template.result)
    if not artwork:
        with store.conn:
            store.conn.execute('UPDATE youtube_items SET thumbnail=NULL')
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 600})
    path = f"/playlist/{playlist.id}" if kind == "playlist" else f"/album?browse={browse}"
    page.goto(similar_app + path)
    # Album and playlist order can differ. Exercise the actual last rendered row.
    trigger = page.get_by_role("button", name="More actions for", exact=False).last
    trigger.scroll_into_view_if_needed()
    # scrollIntoView may return before the page's smooth scroll has advanced.
    # Wait for the actual scrolled state before opening the modal.
    page.wait_for_function("""el => {
        let distance = window.scrollY;
        for (let parent = el.parentElement; parent; parent = parent.parentElement)
            distance = Math.max(distance, parent.scrollTop);
        return distance > 200;
    }""", arg=trigger.element_handle())
    trigger.click()
    page.get_by_role("menuitem", name="Songs like this", exact=True).click()
    dialog = page.locator("#similar-modal .modal-similar")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_css("transform", "none")
    expect(dialog.locator(".alt-row")).to_have_count(12)
    yield dialog, kind, width, artwork
    assert errors == []


def test_similar_results_scroll_without_clipping_actions(similar_dialog, page):
    dialog, kind, width, artwork = similar_dialog
    bounds = dialog.bounding_box()
    assert bounds is not None
    assert 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= width
    assert 0 <= bounds["y"] and bounds["y"] + bounds["height"] <= 600
    results = dialog.locator("#similar-results")
    rows = results.locator(".alt-row")
    expect(rows).to_have_count(12)
    dimensions = results.evaluate("el => ({w: el.clientWidth, sw: el.scrollWidth, h: el.clientHeight, sh: el.scrollHeight})")
    assert dimensions["sh"] > dimensions["h"], "The fixture must exercise vertical scrolling"
    assert dimensions["sw"] <= dimensions["w"], "Results must not require horizontal scrolling"
    expect(rows.first.get_by_role("link", name="Play", exact=True)).to_be_in_viewport(ratio=1)
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_in_viewport()
    if kind == "playlist":
        expect(dialog.get_by_role("button", name="Add to playlist", exact=True)).to_be_in_viewport()
        checkbox = rows.first.get_by_role("checkbox")
        checkbox.check()
        expect(checkbox).to_be_checked()
    results.evaluate("el => el.scrollTop = el.scrollHeight")
    expect(rows.last.get_by_role("link", name="Play", exact=True)).to_be_in_viewport(ratio=1)
    images = rows.locator("img")
    expect(images).to_have_count(12 if artwork else 0)
    if artwork:
        page.wait_for_function("""() => [...document.querySelectorAll('#similar-results img')]
            .every(img => img.complete && img.naturalWidth > 0)""")
        # The real error handler removes failed artwork; actions must still fit after it disappears.
        images.evaluate_all("imgs => imgs.forEach(img => img.dispatchEvent(new Event('error')))")
        expect(images).to_have_count(0)
        dimensions = results.evaluate("el => ({w: el.clientWidth, sw: el.scrollWidth})")
        assert dimensions["sw"] <= dimensions["w"]
        results.evaluate("el => el.scrollTop = 0")
        expect(rows.first.get_by_role("link", name="Play", exact=True)).to_be_in_viewport(ratio=1)
        results.evaluate("el => el.scrollTop = el.scrollHeight")
        expect(rows.last.get_by_role("link", name="Play", exact=True)).to_be_in_viewport(ratio=1)
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
