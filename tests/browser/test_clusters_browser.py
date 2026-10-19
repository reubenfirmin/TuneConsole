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
            store.upsert_track(f"v{a}-{i}", title, artist, f"Album {a}-{i // 2}", 180,
                               thumbnail="/static/nag/intro.png")
            key = identity_key(title, artist)
            angle = math.radians(a * 65 + i)
            rows.append((key, np.array([math.cos(angle), math.sin(angle)], dtype=np.float32).tobytes()))
    store.replace_rec_vectors(rows)
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
