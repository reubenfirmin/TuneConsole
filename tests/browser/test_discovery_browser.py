"""Discovery counts require exposure, including when tabs, polls, and grid clamps hide tiles."""
import pytest
from playwright.sync_api import expect
from tests.browser_clock_support import discovery_dwell, discovery_interval
from tests.home_fixture_support import publish_menu, returning_listener
from tests.visual_support import capture
from yt_playlist.rec import home_prepared

pytestmark = pytest.mark.usefixtures('unlocked_home_controls')


@pytest.mark.parametrize("width", [1440])
def test_only_visible_discovery_tiles_count_once_per_visit(live_app, store, page, width, tmp_path):
    store.settings.set_setting("last_sync_at", "1")
    returning_listener(store, 'mixes')
    for i in range(15):
        store.discovery.upsert_discovered_album(f"album{i}", f"Artist {i}", f"Album {i}", "2026", None, 1.0)
    store.discovery.upsert_discovered_artist("New artist", 1.0, [], [], None, 1.0)
    publish_menu(store, 1)
    assert home_prepared.prepare_discovery(store, 1) is not None
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.set_viewport_size({"width": width, "height": 1000})
    page.clock.install()
    page.goto(f"{live_app}/")
    expect(page.get_by_role("tab", name="Mix tapes", exact=True)).to_be_visible()
    tiles = page.locator("#album-grid > [data-discovery-id]")
    expect(tiles).to_have_count(0)
    assert not any('panel=discover&fragment=1' in url or url.endswith('/home/discovery') for url in requests)
    assert all(a["offered_count"] == 0 for a in store.discovery.get_discovered_albums())

    with page.expect_response('**/?panel=discover&fragment=1') as opened:
        page.get_by_role("tab", name="Discover", exact=True).click()
        page.clock.run_for(50)  # Deliver Alpine's deferred panel reveal under the paused clock.
    assert opened.value.ok
    expect(tiles).to_have_count(15)
    tiles.first.scroll_into_view_if_needed()
    discovery_dwell(page, tiles.first)
    shown = {a["browse_id"]: a["offered_count"] for a in store.discovery.get_discovered_albums()}
    assert any(shown.values()) and max(shown.values()) == 1
    hidden_ids = tiles.evaluate_all("tiles => tiles.filter(t => !t.getClientRects().length).map(t => t.dataset.discoveryId)")
    assert all(shown[item_id] == 0 for item_id in hidden_ids)

    with page.expect_response('**/home/discovery'):
        page.evaluate("""() => htmx.ajax('GET', '/home/discovery', {
            target: '#home-discovery', swap: 'outerHTML'})""")
    expect(tiles).to_have_count(15)
    discovery_interval(page, tiles.first)
    assert max(a["offered_count"] for a in store.discovery.get_discovered_albums()) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    capture(page, path=tmp_path / f"discovery-{width}.png", full_page=True)
    artist = page.locator("#new-artist-grid [data-discovery-id]").first
    artist.scroll_into_view_if_needed()
    discovery_dwell(page, artist)
    assert store.discovery.get_discovered_artists()[0]["offered_count"] == 1


def test_pool_shows_metadata_charts_above_tracks(live_app, store, page):
    store.discovery.upsert_discovered_track("cold", "v1", "Cold Song", "Artist", "Album", None,
                                  "House", "2020", "album", 1,
                                  audio={"bpm": 128.0, "danceability": .7})
    store.enrichment_state.record_enrichment_attempt("cold", "musicbrainz", {"genre": "House"}, now=100)
    store.enrichment_state.record_enrichment_attempt("cold", "deezer", {}, retry=True, now=101)
    store.discovery.upsert_discovered_track("unknown", "v2", "Unknown Song", "Artist", None, None,
                                  None, None, "radio:v", 2)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{live_app}/discovery")
    row = page.locator("tr").filter(has_text="Cold Song")
    expect(row).to_contain_text("House · 2020 · 128.0 BPM · Danceability 0.7")
    overview = page.get_by_label("Track pool overview")
    expect(overview).to_be_visible()
    for caption in ("By genre", "By era", "Unenriched"):
        expect(overview.get_by_text(caption, exact=True)).to_be_visible()
    expect(overview.locator(".discovery-chart-total")).to_have_text("1")
    expect(page.get_by_text("Provider details", exact=False)).to_have_count(0)
    for width in (1440, 1920):
        page.set_viewport_size({"width": width, "height": 1000})
        assert overview.bounding_box()["y"] < row.bounding_box()["y"]
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        capture(page, path=f'build/test-reports/home-discovery-browser/discovery-pool-{width}.png', full_page=True)
    page.get_by_role("button", name="Albums (0)").click()
    expect(overview).to_be_hidden()


@pytest.mark.parametrize("width", [1440])
def test_prepared_songs_are_ready_when_discover_opens(live_app, store, page, width, tmp_path, monkeypatch):
    from yt_playlist.rec import fresh_pool

    store.settings.set_setting("last_sync_at", "1")
    returning_listener(store, 'mixes')
    for i in range(40):
        store.discovery.upsert_discovered_track(f"new {i}|artist {i}", f"v{i}", f"New song {i}",
            f"Artist {i}", None, None, "Jazz", "2020", "radio:seed", 1)
    fresh_pool.prepare(store, 2)
    assert home_prepared.prepare_discovery(store, 2) is not None
    publish_menu(store, 1)
    # The initial tab fragment must display the saved songs without a follow-up refresh.
    page.route("**/home/discovery", lambda route: route.abort())
    page.set_viewport_size({"width": width, "height": 1000})
    page.clock.install()
    pause_at = page.clock.pause_at
    pause_times = []

    def delayed_pause(at):
        # Inject protocol latency beyond the old 100 ms scheduling margin.
        # This is a race challenge, not a wait for layout or preparation.
        page.wait_for_timeout(250)
        pause_at(at)
        pause_times.append(at)

    monkeypatch.setattr(page.clock, 'pause_at', delayed_pause)
    page.goto(f"{live_app}/")
    tiles = page.locator("#discovery-track-grid > [data-discovery-id]")
    expect(tiles).to_have_count(0)
    # Some songs also appear in Mix tapes; their existing offers belong to that surface.
    before = {row["identity_key"]: row["offered_count"] for row in store.discovery.get_discovered_tracks()}
    assert store.conn.execute('SELECT count(*) FROM recordings r,json_each(r.discovery_views)').fetchone()[0] == 0

    with page.expect_response('**/?panel=discover&fragment=1') as opened:
        page.get_by_role("tab", name="Discover", exact=True).click()
        page.clock.run_for(50)
    assert opened.value.ok
    expect(page.get_by_role("heading", name="Songs to discover")).to_be_visible()
    expect(tiles).to_have_count(10)
    expect(tiles.first).to_be_visible()
    expect(page.get_by_text("Finding new music for you…", exact=True)).to_have_count(0)
    title = tiles.first.locator("a.tile-cover")
    assert title.get_attribute("href").startswith("/track/v0?")
    expect(tiles.first.locator("a.tile-play")).to_have_attribute("href", "https://music.youtube.com/watch?v=v0")
    discovery_dwell(page, tiles.first)
    deltas = [row["offered_count"] - before[row["identity_key"]] for row in store.discovery.get_discovered_tracks()]
    assert max(deltas) == 1
    discovery_interval(page, tiles.first)
    assert page.evaluate('Date.now() / 1000') == pytest.approx(pause_times[0] + 1.15, rel=0, abs=.001)
    assert max(row['offered_count'] - before[row['identity_key']]
               for row in store.discovery.get_discovered_tracks()) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    capture(page, path=tmp_path / f"discovery-ready-songs-{width}.png", full_page=True)


def test_visible_discovery_survives_a_model_reset_and_background_refresh(live_app, store, page):
    store.settings.set_setting('last_sync_at', '1')
    returning_listener(store, 'mixes')
    store.discovery.upsert_discovered_album('retained', 'Artist', 'Still here after refresh', '2026', None, 1)
    publish_menu(store, 1)
    assert home_prepared.prepare_discovery(store, 1) is not None
    page.goto(live_app + '/')
    page.get_by_role('tab', name='Discover', exact=True).click()
    tile = page.locator('#album-grid [data-discovery-id="retained"]')
    expect(tile).to_be_visible()
    with store.conn.atomic():
        from yt_playlist.core import home_outputs
        home_outputs.clear(store.conn, ('home_prepared', 'home_tuned', 'home_choice'))
        store.conn.execute("DELETE FROM rec_proposals WHERE surface IN "
                           "('home_prepared','home_tuned','home_choice','mode_bundles','fresh_pool')")
    with page.expect_response('**/home/discovery') as refreshed:
        page.evaluate("""() => htmx.ajax('GET', '/home/discovery', {
            target: '#home-discovery', swap: 'outerHTML'})""")
    assert refreshed.value.ok
    expect(tile).to_be_visible()
    expect(page.get_by_text('Finding new music for you…', exact=True)).to_have_count(0)
    capture(page, path='/tmp/tuneconsole-417-discovery-after-refresh.png', full_page=True)
    page.reload()
    page.get_by_role('tab', name='Discover', exact=True).click()
    expect(tile).to_be_visible()


def test_missing_artist_art_appears_on_the_existing_discover_page(live_app, store, page, monkeypatch):
    from types import SimpleNamespace
    from yt_playlist.rec import discover
    from yt_playlist.rec.rec_worker import RecWorker

    store.settings.set_setting('last_sync_at', '1')
    returning_listener(store, 'mixes')
    store.discovery.upsert_discovered_artist('New artist', 1, [], [], None, 1)
    assert home_prepared.prepare_discovery(store, 1) is not None
    queued = []

    def queue(worker, ids):
        queued.append((worker, ids))
        worker._model_active = True

    monkeypatch.setattr(RecWorker, 'trigger_artist_images', queue)
    page.goto(live_app + '/')
    page.get_by_role('tab', name='Discover', exact=True).click()
    tile = page.locator('#new-artist-grid .tile')
    tile.scroll_into_view_if_needed()
    expect(tile.locator('img')).to_have_count(0)
    assert queued
    worker, ids = queued[-1]
    client = SimpleNamespace(search=lambda name, filter: [
        {'thumbnails': [{'url': live_app + '/static/favicon.svg'}]}])
    ctx = SimpleNamespace(store=store, client_provider=lambda: {1: client})
    discover.repair_artist_images(ctx, ids, 1)
    worker._model_active = False

    expect(tile.locator('img')).to_have_count(1, timeout=10000)
    expect(tile.locator('.tile-cover')).not_to_have_class('tile-cover no-art')
    page.wait_for_function("""() => {
        const image = document.querySelector('#new-artist-grid img');
        return image && image.complete && image.naturalWidth > 0;
    }""")
    assert store.discovery.get_discovered_artists()[0]['thumbnail'].endswith('/static/favicon.svg')
