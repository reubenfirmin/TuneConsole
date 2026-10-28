"""Feature assets must initialize their own pages without leaking into unrelated pages."""
from pathlib import Path

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("path,feature", [
    ("/", "home"), ("/playlists", None), ("/setup", "setup"),
    ("/road_trip", "road-trip"), ("/clusters", "cluster"), ("/charts", None),
    ("/taste", None), ("/cleanup", None), ("/move", None),
    ("/albums", None), ("/artists", None),
])
def test_feature_loading_and_responsive_layout(page, live_app, path, feature, width):
    page.set_viewport_size({"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    response = page.goto(live_app + path)
    assert response.status == 200
    page.wait_for_function("window.Alpine && document.querySelector('[x-data]')._x_dataStack")
    scripts = page.locator("script[src]").evaluate_all(
        "els => els.map(el => new URL(el.src).pathname.split('/').pop())")
    for name in ("home", "road-trip", "setup", "cluster"):
        assert (f"{name}.js" in scripts) == (name == feature)
    assert ("force-graph.min.js" in scripts) == (feature == "cluster")
    for helper in ("cluster-state.js", "cluster-storage.js", "cluster-renderer.js"):
        assert (helper in scripts) == (feature == "cluster")
        if feature == "cluster":
            assert scripts.index(helper) < scripts.index("cluster.js") < scripts.index("alpine.min.js")
    for helper in ("home-board.js", "home-tuning.js", "home-discovery.js"):
        assert (helper in scripts) == (feature == "home")
    assert page.evaluate("typeof genOpenYT") == ("function" if path in ("/", "/road_trip") else "undefined")
    for factory in ("playerStatus", "genreDialog", "omniSearch"):
        assert page.evaluate(f"typeof {factory}") == "function"
    assert ("table-controls.js" in scripts) == (path == "/" or path.startswith(
        ("/playlist", "/album", "/artist", "/cleanup", "/taste")))
    assert ("library-playlists.js" in scripts) == path.startswith(("/playlist", "/move"))
    assert ("enrichment-panel.js" in scripts) == path.startswith(("/playlist/", "/album"))
    for script in scripts:
        if script.endswith(".js") and script != "alpine.min.js":
            assert scripts.index(script) < scripts.index("alpine.min.js")
    styles = page.locator('link[rel="stylesheet"]').evaluate_all(
        "els => els.map(el => new URL(el.href).pathname.split('/').pop())")
    assert styles[:3] == ["tokens.css", "app.css", "components.css"]
    assert "page-overrides.css" not in styles
    assert ("clusters.css" in styles) == (feature == "cluster")
    for owner in ("home.css", "home-board.css", "home-tuning.css"):
        assert (owner in styles) == (feature == "home")
    assert page.locator("main").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), page.evaluate("""() =>
        JSON.stringify([...document.querySelectorAll('main *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1)
        .slice(-24).map(el => [el.tagName, el.className, el.getBoundingClientRect().right]), null, 2)""")
    if path == "/playlists" and width == 390:
        assert page.locator(".library-page-head").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns.split(' ').length") == 1
    assert errors == []


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("path", ["/setup", "/enrich", "/charts", "/playlists"])
def test_connected_player_keeps_shared_layout_without_home_assets(page, live_app, path, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/playback-cover.svg", lambda route: route.fulfill(
        content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" '
        'width="300" height="300"><rect width="300" height="300" fill="purple"/></svg>'))
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {"title": "Playback check", "artist": "Test artist",
            "video_id": "PLAYBACK", "paused": False, "likeStatus": "INDIFFERENT",
            "thumbnail": live_app + "/playback-cover.svg"}}))
    page.goto(live_app + path)
    player = page.locator(".global-now-playing")
    expect(player).to_be_visible()
    expect(player.get_by_role("button", name="Pause", exact=True)).to_be_visible()
    artwork = player.locator(".hs-art")
    expect(artwork).to_be_visible()
    page.wait_for_function("document.querySelector('.global-now-playing .hs-art').naturalWidth === 300")
    box = artwork.bounding_box()
    assert box["width"] == pytest.approx(48)
    assert box["height"] == pytest.approx(box["width"])
    styles = page.locator('link[rel="stylesheet"]').evaluate_all(
        "els => els.map(el => new URL(el.href).pathname.split('/').pop())")
    assert "playback.css" in styles
    if path != "/playlists":
        assert "home.css" not in styles
    if width == 1440:
        expect(page.locator(".sidebar-dock-slot .global-now-playing")).to_be_visible()
        assert player.locator(".hs-now").evaluate("el => getComputedStyle(el).display") == "grid"
    else:
        expect(page.locator(".global-now-playing-slot .global-now-playing")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def test_removed_home_status_does_not_reopen_pending_sync_stream(page):
    page.goto("about:blank")
    page.add_script_tag(path=str(Path(__file__).parents[2] / "src/yt_playlist/web/static/playback-status.js"))
    page.add_script_tag(path=str(Path(__file__).parents[2] / "src/yt_playlist/web/static/home.js"))
    assert page.evaluate("""async () => {
        let resolveScan;
        window.fetch = url => url === '/sync/active'
            ? new Promise(resolve => { resolveScan = resolve; })
            : Promise.resolve({json: async () => ({connected: true})});
        let opened = 0;
        window.EventSource = class { constructor() { opened++; } };
        const status = homeStatus();
        status.$el = document.body;
        status.init();
        status.destroy();
        resolveScan({json: async () => ({job_id: 10})});
        await new Promise(resolve => setTimeout(resolve, 0));
        return opened === 0 && status.connected === false;
    }""")


@pytest.mark.parametrize("width", [390, 1440])
def test_album_document_loads_conflict_badge_and_resolver_styles(page, live_app, store, width):
    track = store.tracks.upsert_track("album-song", "Album song", "Artist", "Album", 200,
                                     album_browse_id="ALBUM")
    store.collection.replace_saved_albums([
        {"browse": "ALBUM", "title": "Album", "artist": "Artist", "year": "2000",
         "type": "Album", "thumbnail": None}])
    store.enrichment.upsert_conflict(track, "genre", [
        {"provider": "musicbrainz", "value": "Rock"}, {"provider": "discogs", "value": "Pop"}])
    page.set_viewport_size({"width": width, "height": 900})
    response = page.goto(live_app + "/album?browse=ALBUM")
    assert response.status == 200
    expect(page.locator('link[href*="enrichment-conflicts.css"]')).to_have_count(1)
    badge = page.locator(".conflict-badge")
    expect(badge).to_be_visible()
    assert badge.evaluate("el => getComputedStyle(el).position") == "absolute"
    button = page.locator(".conflict-icon")
    assert button.evaluate("el => getComputedStyle(el).position") == "relative"
    button.click()
    modal = page.locator(".modal-wide")
    expect(modal).to_be_visible()
    assert modal.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    assert modal.evaluate("el => getComputedStyle(el).maxWidth") == "640px"
    assert page.locator(".conflict-options").evaluate("el => getComputedStyle(el).display") == "flex"
    box = modal.bounding_box()
    assert box["width"] <= min(width * .92, 640) + 1 and box["height"] <= 900 * .82 + 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), page.evaluate("""() =>
        JSON.stringify([...document.querySelectorAll('main *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1)
        .slice(-24).map(el => [el.tagName, el.className, el.getBoundingClientRect().right]), null, 2)""")


def test_htmx_removal_releases_playlist_sortable(page, live_app, store):
    playlist = store.conn.execute("SELECT id FROM playlists").fetchone()[0]
    page.goto(f"{live_app}/playlist/{playlist}")
    page.wait_for_function("""() => {
        const root = document.querySelector('[x-data^="rowSort("]');
        return root && Alpine.$data(root)._sortable;
    }""")
    page.evaluate("""() => {
        const root = document.querySelector('[x-data^="rowSort("]');
        const sortable = Alpine.$data(root)._sortable;
        window.destroyedSortables = 0;
        const destroy = sortable.destroy.bind(sortable);
        sortable.destroy = () => { window.destroyedSortables++; destroy(); };
        htmx.swap(root, '<div>Playlist closed</div>', {swapStyle: 'outerHTML', settleDelay: 0});
    }""")
    page.wait_for_function("window.destroyedSortables === 1")
