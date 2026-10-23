"""Artist seed rings, accessible seed editing, and readable multi-seed canvas cards."""
import math

import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.util.matching import identity_key


ARTISTS = ["Northern Lights and the Orchestra of Distant Stars",
           "The Extremely Long Artist Name from Another Hemisphere"]
SONG = "A very long song title that must wrap inside the central cluster without disappearing off to the right"
DATA = "Alpine.$data(document.querySelector('.cluster-page'))"


@pytest.fixture
def cluster_library(store, live_app):
    rows = []
    for a, artist in enumerate(ARTISTS + ["Related Artist"]):
        for i in range(12):
            title = SONG if a == 0 and i == 0 else f"Song {a}-{i}"
            store.tracks.upsert_track(f"v{a}-{i}", title, artist, f"Album {a}-{i // 2}", 180,
                               thumbnail="/static/nag/intro.png")
            key = identity_key(title, artist)
            angle = math.radians(a * 65 + i)
            rows.append((key, np.array([math.cos(angle), math.sin(angle)], dtype=np.float32).tobytes()))
    store.rec_model.replace_rec_vectors(rows)
    return live_app


def _add(page, label, kind):
    page.get_by_role("textbox", name="Add artists, playlists, or songs to the central cluster").fill(label)
    page.locator(f".cluster-suggest li:has(.cs-kind.{kind})").filter(has_text=label).first.click()
    expect(page.locator(".cluster-center")).to_have_attribute("aria-busy", "false")


@pytest.mark.parametrize("width", [1440, 600])
def test_seed_songs_wrapping_and_restore(page, cluster_library, width):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1050})
    page.goto(f"{cluster_library}/clusters")
    expect(page.locator(".cluster-graph canvas")).to_have_count(1)
    _add(page, ARTISTS[0], "artist")
    name = page.locator('.cluster-savebar input[name="name"]')
    expect(name).to_have_value(ARTISTS[0] + " cluster")
    assert page.evaluate(f"{DATA}.nodes.filter(n => n.kind === 'track').every(n => n.sub === {ARTISTS[:1]!r}[0])")
    page.locator(".cluster-center").get_by_role("button", name="Edit seeds").click()
    expect(page.get_by_role("region", name="Edit central cluster")).to_be_visible()
    expect(page.get_by_role("textbox", name="Add artists, playlists, or songs to the central cluster")).to_be_focused()
    _add(page, ARTISTS[1], "artist")
    expect(name).to_have_value(" + ".join(ARTISTS) + " cluster")
    artists = page.evaluate(f"{DATA}.nodes.filter(n => n.kind === 'track').map(n => n.sub)")
    assert artists.count(ARTISTS[0]) == 3 and artists.count(ARTISTS[1]) == 3
    _add(page, SONG, "song")
    expected_name = " + ".join(ARTISTS + [SONG]) + " cluster"
    expect(name).to_have_value(expected_name)
    center = page.locator(".cluster-center")
    expect(center.locator(".cn-seed")).to_have_count(3)
    assert center.evaluate("el => el.scrollWidth <= el.clientWidth")
    assert center.locator(".cn-seeds").evaluate("el => el.scrollWidth <= el.clientWidth")
    assert center.evaluate("el => el.offsetHeight > 140")
    assert center.locator(".cn-seed").last.evaluate("el => el.offsetHeight > 30")
    assert page.evaluate(f"{DATA}.nodes.filter(n => n.kind === 'track').every(n => n.label !== {SONG!r})")
    page.get_by_role("region", name="Edit central cluster").get_by_role("button", name="Done").click()
    saved_seed = page.evaluate(f"{DATA}.randomSeed")
    page.wait_for_function(f"!{DATA}._autofit && {DATA}._fg.zoom() <= 1.21")
    page.wait_for_timeout(450)  # let the 400ms fit animation finish before capturing its saved view
    saved_zoom = page.evaluate(f"{DATA}._fg.zoom()")
    page.evaluate(f"{DATA}._flushState()")
    page.reload()
    expect(page.locator(".cluster-center .cn-seed")).to_have_count(3)
    expect(name).to_have_value(expected_name)
    assert page.evaluate(f"{DATA}.randomSeed") == saved_seed
    page.wait_for_function(f"{DATA}._pendingView == null", timeout=5000)
    restored = page.evaluate(f"({{zoom: {DATA}._fg.zoom(), view: JSON.parse(localStorage.getItem({DATA}._canvasKey({DATA}.activeId))).view}})")
    assert abs(restored["zoom"] - saved_zoom) < 0.01, (saved_zoom, restored)
    bounds = center.bounding_box()
    assert 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= width
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_removing_an_artist_keeps_developed_branches(page, cluster_library):
    page.set_viewport_size({"width": 1440, "height": 1050})
    page.goto(f"{cluster_library}/clusters")
    _add(page, ARTISTS[0], "artist")
    _add(page, ARTISTS[1], "artist")
    branch = page.evaluate(f"""async () => {{
      const c = {DATA}, parent = c.nodes.find(n => n.kind === 'track');
      await c.grow(parent.id);
      const pinned = c.children(parent.id)[0];
      pinned.fx = pinned.x; pinned.fy = pinned.y;
      return {{ id: parent.id, pinned: pinned.key }};
    }}""")
    assert branch["pinned"]
    page.locator(".cluster-center").get_by_role("button", name="Edit seeds").click()
    page.get_by_role("button", name="Remove " + ARTISTS[1], exact=True).click()
    expect(page.locator(".cluster-center")).to_have_attribute("aria-busy", "false")
    expect(page.locator(".cluster-center .cn-seed")).to_have_count(1)
    expect(page.locator('.cluster-savebar input[name="name"]')).to_have_value(ARTISTS[0] + " cluster")
    remaining = page.evaluate(f"{DATA}.nodes.map(n => n.key)")
    assert branch["pinned"] in remaining
    assert page.evaluate(f"{DATA}.nodes.some(n => n.id === {branch['id']})")
    page.get_by_role("region", name="Edit central cluster").get_by_role("button", name="Done").click()
    # A custom title survives subsequent seed changes and reopening the canvas.
    name = page.locator('.cluster-savebar input[name="name"]')
    name.fill("Late night mix")
    _add(page, ARTISTS[1], "artist")
    expect(name).to_have_value("Late night mix")
    page.evaluate(f"{DATA}._flushState()")
    page.reload()
    expect(name).to_have_value("Late night mix")


@pytest.fixture
def curated_canvas(page, cluster_library):
    page.goto(f"{cluster_library}/clusters")
    expect(page.locator(".cluster-graph canvas")).to_have_count(1)
    page.evaluate(f"""() => {{
      const c = {DATA};
      const track = (id, parentId, extra = {{}}) => ({{id, parentId, kind: 'track',
        key: 'key' + id, label: 'Song ' + id, sub: 'Artist', vid: 'v' + id,
        depth: parentId === 1 ? 1 : 2, state: 'neutral', x: id * 100, y: 0, ...extra}});
      c._applyState({{v: 1, nextId: 9, rootId: 1, playlistName: 'Curated canvas',
        randomSeed: 42, allowedFamilies: ['Techno'], boosted: ['key5'], trunk: [2, 3, 7],
        nodes: [{{id: 1, parentId: null, kind: 'central', state: 'central', depth: 0,
          x: 0, y: 0, fx: 0, fy: 0, keys: ['seedA', 'seedB'],
          seeds: [{{kind: 'artist', label: 'A', keys: ['seedA']}},
                  {{kind: 'artist', label: 'B', keys: ['seedB']}}]}},
          track(2, 1), track(3, 1), track(4, 1, {{state: 'pruned'}}),
          track(5, 1), track(6, 1, {{fx: 600, fy: 0, _userPinned: true}}),
          track(7, 2), track(8, 2, {{state: 'pruned'}})]}});
      window.canvasShape = saved => {{
        const s = saved ? JSON.parse(localStorage.getItem(c._canvasKey(c.activeId))) : c._stateBlob();
        return {{nodes: s.nodes.map(n => ({{id: n.id, parentId: n.parentId, key: n.key,
            state: n.state, seeds: n.seeds, pinned: n._userPinned ? [n.fx, n.fy] : null}})),
          nextId: s.nextId, rootId: s.rootId, trunk: s.trunk, boosted: s.boosted,
          subHues: s.subHues, allowedFamilies: s.allowedFamilies, playlistName: s.playlistName}};
      }};
      window.changeSeeds = action => action === 'add'
        ? c.addSeed({{kind: 'artist', label: 'C', keys: ['seedC']}}) : c.removeSeed(1);
    }}""")
    page.wait_for_function(f"{DATA}.nodes.filter(n => n.kind === 'track' && !n._userPinned).every(n => n.fx == null)")
    page.evaluate(f"{DATA}.persist(); {DATA}._flushState()")


@pytest.mark.parametrize("action,failure,fail_at", [
    ("add", "http", 1), ("remove", "http", 1),
    ("add", "network", 1), ("remove", "json", 1),
    ("add", "missing", 1), ("remove", "invalid", 1),
    ("add", "http", 2), ("remove", "http", 2),
])
def test_refinement_failure_preserves_canvas_and_allows_retry(page, curated_canvas, action, failure, fail_at):
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    before = page.evaluate("canvasShape(false)")
    assert page.evaluate("canvasShape(true)") == before

    def respond(route):
        requests.append(route.request.post_data_json)
        if len(requests) != fail_at:
            route.fulfill(json={"ring": [{"key": f"new{len(requests)}", "title": "New song",
                                          "artist": "New artist", "video_id": "new"}]})
        elif failure == "network":
            route.abort()
        elif failure == "http":
            route.fulfill(status=503, json={"ring": []})
        elif failure == "json":
            route.fulfill(content_type="application/json", body="{invalid")
        elif failure == "missing":
            route.fulfill(json={})
        else:
            route.fulfill(json={"ring": [{"title": "No identity"}]})

    page.route("**/clusters/expand", respond)
    page.evaluate("action => changeSeeds(action)", action)
    expect(page.locator(".cluster-error")).to_contain_text("Try again")
    expect(page.locator(".cluster-center")).to_have_attribute("aria-busy", "false")
    assert len(requests) == fail_at
    assert page.evaluate("canvasShape(false)") == before
    page.evaluate(f"{DATA}._flushState()")
    assert page.evaluate("canvasShape(true)") == before
    assert page.evaluate(f"{DATA}.exhaustedIds") == []

    # The next attempt succeeds for every parent; the original pins, prunes and boosts survive.
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.locator(".cluster-center")).to_have_attribute("aria-busy", "false")
    expect(page.locator(".cluster-error")).not_to_be_visible()
    shape = page.evaluate("canvasShape(false)")
    root = next(n for n in shape["nodes"] if n["id"] == 1)
    assert len(root["seeds"]) == (3 if action == "add" else 1)
    for kept in (2, 4, 5, 6, 8):
        assert next(n for n in shape["nodes"] if n["id"] == kept) == next(
            n for n in before["nodes"] if n["id"] == kept)
    assert shape["boosted"] == ["key5"]
    assert shape["trunk"] == [2]
    assert len(requests) == fail_at + 2
    assert not errors


@pytest.mark.parametrize("action", ["add", "remove"])
def test_refinement_accepts_authoritative_empty_rings(page, curated_canvas, action):
    page.route("**/clusters/expand", lambda route: route.fulfill(json={"ring": []}))
    page.evaluate("action => changeSeeds(action)", action)
    expect(page.locator(".cluster-error")).not_to_be_visible()
    shape = page.evaluate("canvasShape(false)")
    assert [n["id"] for n in shape["nodes"]] == [1, 2, 4, 5, 6, 8]
    assert shape["trunk"] == [2]
    page.evaluate(f"{DATA}._flushState()")
    assert page.evaluate("canvasShape(true)") == shape


def test_refinement_does_not_overwrite_curation_during_fetch(page, curated_canvas):
    # A real edit during the request invalidates the draft and remains in the saved canvas.
    page.route("**/clusters/expand", lambda route: route.fulfill(json={"ring": []}))
    page.evaluate(f"""async () => {{
      const c = {DATA}, pending = c.addSeed({{kind: 'artist', label: 'C', keys: ['seedC']}});
      c.prune(5);
      await pending;
      c._flushState();
    }}""")
    expect(page.locator(".cluster-error")).not_to_be_visible()
    shape = page.evaluate("canvasShape(false)")
    assert next(n for n in shape["nodes"] if n["id"] == 5)["state"] == "pruned"
    assert len(shape["nodes"][0]["seeds"]) == 2
    assert page.evaluate("canvasShape(true)") == shape


def test_failed_growth_is_not_recommendation_exhaustion(page, curated_canvas):
    before = page.evaluate("canvasShape(false)")
    page.route("**/clusters/expand", lambda route: route.fulfill(status=500, json={"ring": []}))
    page.evaluate(f"{DATA}.grow(1)")
    expect(page.locator(".cluster-error")).to_be_visible()
    assert page.evaluate(f"{DATA}.exhaustedIds") == []
    assert page.evaluate("canvasShape(false)") == before
    page.route("**/clusters/expand", lambda route: route.fulfill(json={"ring": []}))
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.locator(".cluster-error")).not_to_be_visible()
    assert page.evaluate(f"{DATA}.exhaustedIds") == [1]


@pytest.mark.parametrize("kind", ["artist", "genre"])
def test_failed_first_seed_does_not_save_an_incomplete_canvas(page, cluster_library, kind):
    page.goto(f"{cluster_library}/clusters")
    expect(page.locator(".cluster-graph canvas")).to_have_count(1)
    page.route("**/clusters/expand", lambda route: route.abort())
    page.evaluate(f"""kind => {{
      const c = {DATA};
      if (kind === 'genre') c.pickFamily('Techno');
      else c.addSeed({{kind: 'artist', label: 'A', keys: ['seedA']}});
    }}""", kind)
    expect(page.locator(".cluster-error")).to_be_visible()
    assert page.evaluate(f"{DATA}.nodes") == []
    assert page.evaluate(f"{DATA}.allowedFamilies") == []
    assert not page.evaluate("Object.keys(localStorage).some(key => key.startsWith('tc:canvas:'))")
    page.route("**/clusters/expand", lambda route: route.fulfill(json={"ring": []}))
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.locator(".cluster-center .cn-seed")).to_have_count(1)
    expect(page.locator(".cluster-error")).not_to_be_visible()
    assert page.evaluate(f"{DATA}.centralSeeds()[0].kind") == kind


def test_refinement_does_not_overwrite_a_new_canvas(page, curated_canvas):
    pending = []
    page.route("**/clusters/expand", lambda route: pending.append(route))
    before = page.evaluate("canvasShape(true)")
    with page.expect_request("**/clusters/expand"):
        page.evaluate("() => { window.pendingRefinement = changeSeeds('add'); }")
    old_id = page.evaluate(f"{DATA}.activeId")
    page.evaluate(f"{DATA}.newCanvas()")
    pending[0].fulfill(json={"ring": []})
    page.evaluate("pendingRefinement")
    assert page.evaluate(f"{DATA}.nodes") == []
    assert page.evaluate(f"{DATA}.activeId") != old_id
    expect(page.locator(".cluster-error")).not_to_be_visible()
    assert page.evaluate(f"{DATA}.seedBusy") is False
    # Switching canvases flushes the old, usable canvas before leaving it.
    saved = page.evaluate("id => JSON.parse(localStorage.getItem('tc:canvas:' + id))", old_id)
    assert len(saved["nodes"]) == len(before["nodes"])
    assert saved["trunk"] == before["trunk"]


@pytest.mark.parametrize("change", ["replace", "roundtrip", "prune"])
@pytest.mark.parametrize("failure", [False, True])
def test_growth_discards_stale_success_and_failure(page, curated_canvas, change, failure):
    pending = []
    page.route("**/clusters/expand", lambda route: pending.append(route))
    with page.expect_request("**/clusters/expand"):
        page.evaluate(f"() => {{ window.pendingGrowth = {DATA}.grow(2); }}")
    page.evaluate(f"""change => {{
      const c = {DATA}, oldId = c.activeId, state = c._stateBlob();
      if (change === 'prune') c.prune(2);
      else {{
        c.newCanvas();
        if (change === 'roundtrip') c.switchCanvas(oldId);
        else c._applyState(state); // A different canvas reuses the same numeric node IDs.
      }}
      c.clusterError = 'Current error';
      c.retryAction = {{type: 'grow', nodeId: 5}};
    }}""", change)
    before = page.evaluate("canvasShape(false)")
    pending[0].fulfill(status=503 if failure else 200, json={"ring": [
        {"key": "late", "title": "Late response", "artist": "Artist", "video_id": "late"}]})
    page.evaluate("pendingGrowth")
    assert page.evaluate("canvasShape(false)") == before
    assert page.evaluate(f"{DATA}.clusterError") == "Current error"
    assert page.evaluate(f"{DATA}.retryAction") == {"type": "grow", "nodeId": 5}
    assert page.evaluate(f"{DATA}.exhaustedIds") == []
    page.evaluate(f"{DATA}.persist(); {DATA}._flushState()")
    assert page.evaluate("canvasShape(true)") == before


@pytest.mark.parametrize("change", ["empty", "replace", "roundtrip", "prune"])
def test_deep_link_search_cannot_seed_a_replacement_canvas(page, curated_canvas, change):
    pending, expansions = [], []
    page.route("**/clusters/search?*", lambda route: pending.append(route))

    def expand(route):
        expansions.append(route.request.post_data_json)
        route.fulfill(json={"ring": []})

    page.route("**/clusters/expand", expand)
    with page.expect_request("**/clusters/search?*"):
        page.evaluate(f"""() => {{
          window.pendingDeepLink = {DATA}._deepLink('Old linked artist', null, 1, 'Old link');
        }}""")
    page.evaluate(f"""change => {{
      const c = {DATA};
      const oldId = c.activeId, state = c._stateBlob();
      if (change === 'prune') c.prune(2);
      else {{
        c.newCanvas();
        if (change === 'roundtrip') c.switchCanvas(oldId);
        else if (change === 'replace') c._applyState(state);
      }}
      // The user has searched on the replacement canvas, but has not selected its result.
      c.query = 'Replacement artist';
      c.results = [{{kind: 'artist', label: 'Replacement artist', keys: ['replacement']}}];
      c.clusterError = 'Current error'; c.retryAction = {{type: 'grow', nodeId: 5}};
    }}""", change)
    before = page.evaluate("canvasShape(false)")
    pending[0].fulfill(json=[{"kind": "artist", "label": "Old linked artist", "keys": ["old"]}])
    page.evaluate("pendingDeepLink")
    assert page.evaluate("canvasShape(false)") == before
    assert expansions == []
    assert page.evaluate(f"{DATA}.query") == 'Replacement artist'
    assert page.evaluate(f"{DATA}.results[0].label") == 'Replacement artist'
    assert page.evaluate(f"{DATA}.clusterError") == 'Current error'
    assert page.evaluate(f"{DATA}.retryAction") == {"type": "grow", "nodeId": 5}
    if change != "empty":
        page.evaluate(f"{DATA}.persist(); {DATA}._flushState()")
        assert page.evaluate("canvasShape(true)") == before


@pytest.mark.parametrize("phase", ["seed", "growth"])
@pytest.mark.parametrize("change", ["replace", "roundtrip"])
def test_deep_link_stops_after_stale_seed_or_growth(page, curated_canvas, phase, change):
    pending, expansions = [], []
    page.route("**/clusters/search?*", lambda route: route.fulfill(json=[
        {"kind": "artist", "label": "Linked artist", "keys": ["seed"]}]))

    def expand(route):
        expansions.append(route.request.post_data_json)
        if phase == "seed" or len(expansions) == 2:
            pending.append(route)
        else:
            route.fulfill(json={"ring": [
                {"key": "first", "title": "First song", "artist": "Artist", "video_id": "first"}]})

    page.route("**/clusters/expand", expand)
    with page.expect_request(lambda request: request.url.endswith('/clusters/expand') and (
            phase == 'seed' or 'first' in request.post_data_json['pos_keys'])):
        page.evaluate(f"""() => {{
          const c = {DATA}; window.replacementState = JSON.parse(JSON.stringify(c._stateBlob()));
          c.newCanvas();
          window.pendingDeepLink = c._deepLink('Linked artist', null, 3, 'Linked canvas');
        }}""")
    page.evaluate(f"""change => {{
      const c = {DATA}, oldId = c.activeId;
      c.newCanvas();
      if (change === 'roundtrip') c.switchCanvas(oldId);
      else c._applyState(window.replacementState); // includes IDs reused by the old operation
      c.clusterError = 'Current error'; c.retryAction = {{type: 'grow', nodeId: 5}};
    }}""", change)
    before = page.evaluate("canvasShape(false)")
    assert pending
    count = len(expansions)
    pending[0].fulfill(json={"ring": [
        {"key": "late", "title": "Late song", "artist": "Artist", "video_id": "late"}]})
    page.evaluate("pendingDeepLink")
    assert len(expansions) == count
    assert page.evaluate("canvasShape(false)") == before
    assert page.evaluate(f"{DATA}.clusterError") == 'Current error'
    assert page.evaluate(f"{DATA}.retryAction") == {"type": "grow", "nodeId": 5}
    assert page.evaluate(f"{DATA}.seedBusy") is False
    page.evaluate(f"{DATA}.persist(); {DATA}._flushState()")
    if before["nodes"]:
        assert page.evaluate("canvasShape(true)") == before


def test_deep_link_settle_cannot_change_the_replacement_view(page, curated_canvas):
    page.route("**/clusters/search?*", lambda route: route.fulfill(json=[
        {"kind": "artist", "label": "Linked artist", "keys": ["seed"]}]))
    page.route("**/clusters/expand", lambda route: route.fulfill(json={"ring": []}))
    page.evaluate(f"""() => {{
      const c = {DATA};
      window.replacementState = JSON.parse(JSON.stringify(c._stateBlob()));
      c.newCanvas();
      window.realTimeout = window.setTimeout; window.settles = [];
      window.setTimeout = (fn, ms, ...args) => ms === 700
        ? (window.settles.push(() => fn(...args)), 0) : realTimeout(fn, ms, ...args);
      window.pendingDeepLink = c._deepLink('Linked artist', null, 1, 'Linked canvas');
    }}""")
    page.wait_for_function("window.settles.length === 1")
    page.evaluate(f"""() => {{
      const c = {DATA}; c.newCanvas(); c._applyState(window.replacementState);
      c._autofit = false;
    }}""")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    page.evaluate(f"""() => {{
      const c = {DATA}; window.viewResets = 0; c.resetView = () => viewResets++;
      window.setTimeout = realTimeout; window.settles.shift()();
    }}""")
    page.evaluate("pendingDeepLink")
    assert page.evaluate("viewResets") == 0


@pytest.mark.parametrize("failure", [False, True])
def test_shared_state_load_cannot_overwrite_a_replacement_canvas(page, curated_canvas, failure):
    state = page.evaluate(f"{DATA}._stateBlob()")
    pending = []
    page.route("**/clusters/state/SHARED", lambda route: pending.append(route))
    with page.expect_request("**/clusters/state/SHARED"):
        page.evaluate(f"() => {{ window.pendingStateLoad = {DATA}._loadFrom('SHARED'); }}")
    page.evaluate(f"""state => {{
      const c = {DATA}; c.newCanvas(); c._applyState(state);
      c.clusterError = 'Current error'; c.retryAction = {{type: 'grow', nodeId: 5}};
      c.persist(); c._flushState();
      window.beforeState = JSON.stringify(c._stateBlob().nodes.map(n => [n.id, n.key, n.state]));
      window.viewResets = 0; c.resetView = () => viewResets++;
    }}""", state)
    assert pending
    pending[0].fulfill(status=503 if failure else 200, json={**state, "playlistName": "Stale shared name"})
    page.evaluate("pendingStateLoad")
    assert page.evaluate(f"{DATA}.playlistName") == state["playlistName"]
    assert page.evaluate(f"JSON.stringify({DATA}._stateBlob().nodes.map(n => [n.id, n.key, n.state])) === beforeState")
    assert page.evaluate(f"{DATA}.clusterError") == 'Current error'
    assert page.evaluate(f"{DATA}.retryAction") == {"type": "grow", "nodeId": 5}
    assert page.evaluate("viewResets") == 0
    saved = page.evaluate(f"JSON.parse(localStorage.getItem({DATA}._canvasKey({DATA}.activeId)))")
    assert saved["playlistName"] == state["playlistName"]


def test_linked_artist_and_genre_seed_together_without_competing_requests(page, cluster_library):
    from urllib.parse import urlencode

    requests = []

    def expand(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={"ring": []})

    page.route("**/clusters/expand", expand)
    params = urlencode({"seed": ARTISTS[0], "genre": "Techno", "depth": 1, "label": "From Home"})
    page.goto(f"{cluster_library}/clusters?{params}")
    expect(page.locator(".cluster-center .cn-seed")).to_have_text(ARTISTS[0])
    expect(page.locator('.cluster-savebar input[name="name"]')).to_have_value("From Home")
    assert len(requests) == 1
    assert requests[0]["allow_genres"] == ["Techno"]
    assert requests[0]["central_seeds"][0]["kind"] == "artist"
