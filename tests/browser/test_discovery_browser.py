"""Discovery counts require exposure, including when tabs, polls, and grid clamps hide tiles."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("width", [1440, 390])
def test_only_visible_discovery_tiles_count_once_per_visit(live_app, store, page, width, tmp_path):
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
    for i in range(15):
        store.upsert_discovered_album(f"album{i}", f"Artist {i}", f"Album {i}", "2026", None, 1.0)
    store.upsert_discovered_artist("New artist", 1.0, [], [], None, 1.0)
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{live_app}/")
    expect(page.get_by_role("tab", name="Listen", exact=True)).to_be_visible()
    page.wait_for_timeout(1100)  # Exceeds the exposure interval while Discover stays hidden.
    assert not any(url.endswith("/home/discover") for url in requests)
    assert all(a["offered_count"] == 0 for a in store.get_discovered_albums())

    page.get_by_role("tab", name="Discover", exact=True).click()
    tiles = page.locator("#album-grid > [data-discovery-id]")
    expect(tiles).to_have_count(15)
    tiles.first.scroll_into_view_if_needed()
    page.wait_for_timeout(1300)
    shown = {a["browse_id"]: a["offered_count"] for a in store.get_discovered_albums()}
    assert any(shown.values()) and max(shown.values()) == 1
    hidden_ids = tiles.evaluate_all("tiles => tiles.filter(t => !t.getClientRects().length).map(t => t.dataset.discoveryId)")
    assert all(shown[item_id] == 0 for item_id in hidden_ids)
    if width == 390:
        assert hidden_ids  # The mobile grid crops rows that must never count as viewed.

    page.evaluate("""() => htmx.ajax('GET', '/home/discover', {
        target: '[data-dash="discover"] > div:first-child', swap: 'innerHTML'})""")
    expect(tiles).to_have_count(15)
    page.wait_for_timeout(1300)
    assert max(a["offered_count"] for a in store.get_discovered_albums()) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=tmp_path / f"discovery-{width}.png", full_page=True)
    page.locator("#new-artist-grid [data-discovery-id]").first.scroll_into_view_if_needed()
    page.wait_for_timeout(1300)
    assert store.get_discovered_artists()[0]["offered_count"] == 1


@pytest.mark.parametrize("width", [1440, 390])
def test_pool_shows_metadata_charts_above_tracks(live_app, store, page, width, tmp_path):
    store.upsert_discovered_track("cold", "v1", "Cold Song", "Artist", "Album", None,
                                  "House", "2020", "album", 1,
                                  audio={"bpm": 128.0, "energy": .8, "danceability": .7})
    store.record_enrichment_attempt("cold", "musicbrainz", {"genre": "House"}, now=100)
    store.record_enrichment_attempt("cold", "deezer", {}, retry=True, now=101)
    store.upsert_discovered_track("unknown", "v2", "Unknown Song", "Artist", None, None,
                                  None, None, "radio:v", 2)
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{live_app}/discovery")
    row = page.locator("tr").filter(has_text="Cold Song")
    expect(row).to_contain_text("House · 2020 · 128.0 BPM · Energy 0.8 · Danceability 0.7")
    overview = page.get_by_label("Track pool overview")
    expect(overview).to_be_visible()
    for caption in ("By genre", "By era", "Unenriched"):
        expect(overview.get_by_text(caption, exact=True)).to_be_visible()
    expect(overview.locator(".discovery-chart-total")).to_have_text("1")
    assert overview.bounding_box()["y"] < row.bounding_box()["y"]
    expect(page.get_by_text("Provider details", exact=False)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=tmp_path / f"discovery-pool-metadata-{width}.png", full_page=True)
    page.get_by_role("button", name="Albums (0)").click()
    expect(overview).to_be_hidden()
