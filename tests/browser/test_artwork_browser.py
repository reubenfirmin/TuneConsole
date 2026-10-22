"""Entity silhouettes must survive page-specific CSS, HTMX results, and image failures."""
import pytest
from playwright.sync_api import expect

from yt_playlist.util.matching import identity_key


@pytest.fixture
def artwork_library(store, live_app):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PLZ", "Shape Mix", 2, "h", 0.0,
                                thumbnail="/static/nag/intro.png")
    first = store.tracks.upsert_track("v1", "Shape Song", "Shape Artist", "Shape Album", 180,
                               album_browse_id="MPRE_shape", thumbnail="/static/nag/intro.png")
    second = store.tracks.upsert_track("v2", "Missing Song", "Missing Artist", "Missing Album", 180,
                                      album_browse_id="MPRE_missing")
    store.playlists.set_playlist_tracks(pid, [first, second])
    missing_pid = store.playlists.upsert_playlist(iid, "PLMISSING", "Missing Mix", 1, "h", 0.0)
    store.playlists.set_playlist_tracks(missing_pid, [second])
    store.collection.add_saved_album({"browse": "MPRE_shape", "title": "Shape Album", "artist": "Shape Artist",
                           "thumbnail": "/static/nag/lastfm.png"})
    keys = [identity_key("Shape Song", "Shape Artist"), identity_key("Missing Song", "Missing Artist")]
    # live_app's clock is 1.0; a coarse day's noon key must precede that clock.
    store.history.record_history_plays(iid, -86400.0, keys, source_timezone="UTC")
    return live_app, pid


def _shape(locator):
    return locator.evaluate("""el => {
      const s = getComputedStyle(el);
      return [s.borderRadius, s.clipPath];
    }""")


@pytest.mark.parametrize("width", [1440, 600])
def test_shapes_match_between_library_and_search(page, artwork_library, width):
    base, _pid = artwork_library
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{base}/playlists")
    playlist_shape = _shape(page.locator(".pl-cover").first)
    assert playlist_shape == ["12.5%", "none"]
    page.goto(f"{base}/artists")
    artist_shape = _shape(page.locator(".artist-avatar").first)
    assert artist_shape == ["50%", "none"]
    page.goto(f"{base}/albums")
    album_shape = _shape(page.locator(".album-thumb").first)
    assert album_shape == ["0px", "none"]
    if width < 720:  # The existing mobile navigation hides library search.
        return
    page.locator(".omni-input").fill("Shape Artist")
    for kind, expected in [("playlist", playlist_shape), ("artist", artist_shape),
                           ("album", album_shape)]:
        result = page.locator(f".omni-thumb--{kind}").first
        expect(result).to_be_visible()
        assert _shape(result) == expected


@pytest.mark.parametrize("width", [1440, 600])
def test_charts_have_shaped_artwork_and_fallbacks(page, artwork_library, width):
    base, _pid = artwork_library
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{base}/charts")
    for dim, radius, image in [("artist", "50%", "/static/nag/intro.png"),
                               ("album", "0px", "/static/nag/lastfm.png"),
                               ("playlist", "12.5%", "/static/nag/intro.png")]:
        page.get_by_role("button", name=dim.title() + "s", exact=True).click()
        chart = page.locator(f'.ticker[data-dimension="{dim}"]')
        expect(chart).to_be_visible()
        expect(chart.locator(".chart-art")).to_have_count(2)
        art = chart.locator("tbody tr").filter(has_text="Shape").locator(".chart-art")
        expect(art.locator("img")).to_have_attribute("src", image)
        assert _shape(art) == [radius, "none"]
        fallback = chart.locator("tbody tr").filter(has_text="Missing").locator(".chart-art")
        expect(fallback.locator("img")).to_have_count(0)
        expect(fallback.locator("svg")).to_be_visible()
        assert _shape(fallback) == [radius, "none"]
        art.locator("img").evaluate("el => el.dispatchEvent(new Event('error'))")
        expect(art.locator("img")).to_have_count(0)
        expect(art.locator("svg")).to_be_visible()
        assert _shape(art) == [radius, "none"]
        assert chart.locator("thead th").count() == chart.locator("tbody tr").first.locator("td").count()
