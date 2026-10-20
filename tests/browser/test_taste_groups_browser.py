"""Music-group identity, composition and choice uncertainty stay distinct on small screens."""
import numpy as np
import pytest
from playwright.sync_api import expect

from tests.test_mode_profiles import _groups
from yt_playlist.rec import embed


@pytest.mark.parametrize("width", [1920, 1440, 1024, 390, 320])
def test_mix_groups_explain_composition_and_choice_uncertainty(live_app, store, page, width, tmp_path):
    _groups(store)
    keys = []
    vector = np.eye(5, dtype=np.float32)[2]
    for i in range(12):
        tid = store.upsert_track(f"techno-{i}", f"Techno track {i}", "Artist", None, None)
        store.set_track_genre(tid, "Techno")
        store.set_track_year(tid, 2015)
        keys.append(f"techno track {i}|artist")
    modes = store.modes.list_modes()
    modes.append({"mode_id": 3, "label": "techno", "families": [["techno", 12]],
                  "size": 12, "centroid": vector, "rep_keys": keys[:3],
                  "space": embed.content_space_id(store)})
    store.modes.replace_modes(modes, [], 1)
    store.replace_rec_content_vectors(store.get_rec_content_vectors() + [(k, vector.tobytes()) for k in keys])
    for epoch in range(61):
        store.modes.log_impressions(epoch, [("fresh", 3)], 1)
    for pid in range(22):
        store.modes.log_pick(pid, 3, 1)
    store.modes.log_impressions(62, [("fresh", 1)], 1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#generated")
    section = page.locator('[aria-labelledby="generation-learning-title"]')
    expect(section.get_by_role("table", name="Music groups and mix choices")).to_be_visible()
    rows = section.locator(".choice-chart-row")
    expect(rows).to_have_count(3)
    # Untried groups have no score, even though the sampler's unused prior is 50/100.
    expect(rows.locator(".choice-group-head > strong")).to_have_text([
        "Techno · 2010s", "2010s blend", "House · 2020s"])
    expect(rows.nth(0).locator(".choice-interval")).to_contain_text("29–44")
    expect(rows.nth(1).get_by_role("list", name="Genre composition")).to_contain_text("House 40%")
    expect(rows.nth(2).get_by_role("list", name="Genre composition")).to_contain_text("House 100%")
    expect(rows.nth(2).locator(".choice-dot, .choice-band")).to_have_count(0)
    expect(rows.nth(2)).to_contain_text("Untried")
    expect(section.get_by_text("Dot position: choice score", exact=True)).to_be_visible()
    expect(section.get_by_text("Bar width: uncertainty", exact=True)).to_be_visible()
    expect(section.locator("details, input")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width >= 1040:
        main = page.locator("main.page-taste").bounding_box()
        assert abs(main["x"] + main["width"] - width) < 1
    for row in rows.all():
        bounds = row.bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
        if width > 560:
            centers = row.evaluate("""el => Array.from(el.querySelectorAll(
                '.choice-composition, .choice-range, .choice-band, .choice-dot, .choice-estimate > .mono, .choice-count'
            ), item => { const r = item.getBoundingClientRect(); return r.y + r.height / 2; })""")
            assert max(centers) - min(centers) < 1
            legend = row.locator(".choice-composition-key").bounding_box()
            interval = row.locator(".choice-interval").bounding_box()
            assert abs(legend["y"] - interval["y"]) < 1
    page.screenshot(path=str(tmp_path / f"taste-groups-{width}.png"), full_page=True)
    assert errors == []
