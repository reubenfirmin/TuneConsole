"""Learning cards, signal history and saved learning controls."""
import re

import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.rec import rec_params

@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_learning_cards_search_page_and_explain_real_progress(live_app, store, page, width, tmp_path):
    for i in range(15):
        store.rec_signals.bump_theme(f"artist:Artist {i:02}", .3 + i * .03, 1.0)
    store.rec_signals.bump_theme('artist:An artist’s "long" name & AC:DC', -.9, 1.0)
    store.rec_signals.bump_theme("genre:other:alternative metal", .6, 1.0)
    store.rec_signals.bump_theme("era:1990", .9, 1.0)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#learning")
    cards = page.locator(".learning-card")
    expect(cards).to_have_count(6)
    artists = page.locator(".learning-card--artist")
    expect(artists.locator(".learning-entry")).to_have_count(6)
    expect(artists.locator(".learning-selected")).to_contain_text("Evidence toward a reduction")
    expect(artists.locator(".learning-dial-reading strong")).to_have_text("75%")
    expect(artists.get_by_role("button", name="Previous artists", exact=True)).to_be_disabled()
    artists.get_by_role("button", name="Next artists", exact=True).click()
    expect(artists.locator(".learning-pagination")).to_contain_text("7–12 of 16")
    expect(artists.get_by_role("button", name="Previous artists", exact=True)).to_be_enabled()
    search = artists.get_by_role("searchbox", name="Search artists", exact=True)
    search.fill("Artist 00")
    expect(artists.locator(".learning-entry")).to_have_count(1)
    expect(artists.locator(".learning-selected h4")).to_have_text("Artist 00")
    expect(artists.locator(".learning-dial-reading strong")).to_have_text("25%")
    expect(artists.get_by_role("button", name="Next artists", exact=True)).to_be_disabled()
    search.fill("no match")
    expect(artists.locator(".learning-no-results")).to_be_visible()
    expect(artists.locator(".learning-entry")).to_have_count(0)
    search.fill("")
    second = artists.locator(".learning-entry").nth(1)
    second.focus()
    second.press("Enter")
    expect(second).to_have_attribute("aria-pressed", "true")
    expect(artists.locator(".learning-selected")).to_contain_text("Evidence toward a boost")
    expect(page.locator(".learning-card--genre .learning-selected h4")).to_have_text("alternative metal")
    expect(page.locator(".learning-card--era .learning-selected h4")).to_have_text("1990s")
    expect(page.locator(".learning-source")).to_have_count(5)
    expect(page.locator(".learning-progress-cards .learning-entry")).to_have_count(8)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"taste-learning-{width}.png"), full_page=True)
    assert errors == []


@pytest.mark.parametrize("width", [1440, 1024, 320])
def test_learning_direction_filters_reach_negatives_beyond_sixty_pages(live_app, store, page, width, tmp_path):
    now = 1_700_000_000
    for i in range(365):
        store.rec_signals.bump_theme(f"artist:Positive {i:03}", .9, now + 1000 + i)
    for i in range(7):
        store.rec_signals.bump_theme(f"artist:Negative {i:02}", -.12 - i * .012, now + 100 + i)
    store.rec_signals.bump_theme("artist:Negative priority", -.06, now + 200)
    store.rec_signals.bump_theme("artist:Balanced", 0, 0)
    store.rec_signals.bump_theme("genre:jazz", .3, 1.0)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#learning")
    artists = page.locator(".learning-card--artist")
    filters = artists.get_by_role("group", name="Filter artists by learning direction", exact=True)
    boosts = filters.get_by_role("button", name=re.compile("^Boosts"))
    reductions = filters.get_by_role("button", name=re.compile("^Reductions"))
    all_rows = filters.get_by_role("button", name=re.compile("^All"))
    expect(all_rows.locator("strong")).to_have_text("374")
    expect(boosts.locator("strong")).to_have_text("365")
    expect(reductions.locator("strong")).to_have_text("8")
    expect(artists.locator(".learning-entry.is-reduction")).to_have_count(0)
    expect(artists.locator(".learning-entry-name")).to_have_text([f"Positive {i:03}" for i in range(364, 358, -1)])
    artists.get_by_role("button", name="Next artists", exact=True).click()
    reductions.click()
    expect(reductions).to_have_attribute("aria-pressed", "true")
    expect(artists.locator(".learning-entry.is-reduction")).to_have_count(6)
    expect(artists.locator(".learning-pagination")).to_contain_text("1–6 of 8")
    expect(artists.locator(".learning-selected h4")).to_have_text("Negative priority")
    expect(artists.locator(".learning-dial-reading strong")).to_have_text("5%")
    expect(artists.locator(".learning-entry-updated").first).to_have_attribute("datetime", "2023-11-14T22:16:40.000Z")
    expect(artists.locator(".learning-entry-updated").first).to_contain_text("Updated")
    expect(artists.locator(".learning-focus")).to_have_class(re.compile("is-reduction"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    artists.screenshot(path=str(tmp_path / f"learning-reductions-{width}.png"))
    artists.get_by_role("button", name="Next artists", exact=True).click()
    expect(artists.locator(".learning-pagination")).to_contain_text("7–8 of 8")
    boosts.click()
    expect(artists.locator(".learning-pagination")).to_contain_text("1–6 of 365")
    expect(artists.locator(".learning-entry.is-reduction")).to_have_count(0)
    expect(artists.locator(".learning-selected h4")).to_have_text("Positive 364")
    search = artists.get_by_role("searchbox", name="Search artists", exact=True)
    search.fill("Negative 00")
    expect(artists.locator(".learning-no-results")).to_be_visible()
    reductions.focus()
    reductions.press("Enter")
    expect(artists.locator(".learning-entry")).to_have_count(1)
    expect(artists.locator(".learning-selected h4")).to_have_text("Negative 00")
    expect(search).to_have_value("Negative 00")
    expect(reductions.locator("strong")).to_have_text("8")
    all_rows.click()
    search.fill("Balanced")
    expect(artists.locator(".learning-entry-direction")).to_have_text("Balanced")
    expect(artists.locator(".learning-entry-updated")).to_have_attribute("datetime", "1970-01-01T00:00:00.000Z")
    genres = page.locator(".learning-card--genre")
    expect(genres.locator(".learning-entry")).to_have_count(1)
    genres.get_by_role("button", name=re.compile("^Reductions")).click()
    expect(genres.locator(".learning-selected h4")).to_have_text("No reductions building")
    expect(genres.locator(".learning-no-results")).to_have_text("No reductions building.")
    expect(genres.get_by_role("button", name="Next genres", exact=True)).to_be_disabled()
    assert errors == []


def test_learning_empty_state(live_app, page):
    page.goto(live_app + "/taste#learning")
    progress = page.locator(".learning-progress-cards")
    expect(progress.locator(".learning-empty")).to_have_count(3)
    expect(progress.locator(".learning-selected h4")).to_have_text(["No evidence yet"] * 3)
    expect(progress.locator(".learning-entry")).to_have_count(0)
    expect(page.locator(".learning-card--choices .learning-selected h4")).to_have_text("No mix choices yet")
    expect(page.locator(".learning-card--changes .learning-selected h4")).to_have_text("No saved changes yet")
    expect(page.locator(".learning-card--sources .learning-entry")).to_have_count(3)


@pytest.mark.parametrize("width", [1440, 1024, 320])
def test_other_learning_shows_real_choices_sources_and_dated_saved_changes(live_app, store, page, width, tmp_path):
    from yt_playlist.rec import graduation

    store.modes.replace_modes([
        {"mode_id": mid, "label": label, "families": [["house", 1]],
         "centroid": np.array([1., 0.], dtype=np.float32), "size": 20, "rep_keys": []}
        for mid, label in [(1, 'House & "Techno"'), (2, "Acoustic")] +
                         [(mid, f"Group {mid}") for mid in range(3, 9)]], [], 1)
    store.modes.log_impressions(0, [(f"lane{mid}", mid) for mid in range(3, 9)], 5)
    store.modes.log_impressions(1, [("a", 1), ("b", 2)], 10)
    store.modes.log_impressions(2, [("a", 1)], 20)
    store.modes.log_pick(1, 2, 30)
    store.modes.log_pick(2, 2, 40)  # selections can outnumber offers
    store.rec_model.set_weight("lane:deep_cut", 2, now=1)
    graduation.graduate_facet(store, "genre:techno", -2, 200, source_label="dislike")
    for i in range(8):
        graduation.graduate_facet(store, f"artist:Artist {i:02}", 2, 100 + i, source_label="play")
    errors, panels = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: panels.append(request.url) if "/taste/panels/" in request.url else None)
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#learning")
    expect(page.locator(".learning-card")).to_have_count(6)
    choices = page.locator(".learning-card--choices")
    expect(choices.locator(".learning-card-foot")).to_contain_text("2 selections across 9 offers")
    expect(choices.locator(".learning-entry-name")).to_have_text(["Acoustic", 'House & "Techno"'] + [f"Group {mid}" for mid in range(3, 7)])
    expect(choices.locator(".learning-selected")).to_contain_text("2 selected · 1 offered")
    expect(choices.locator(".learning-dial-reading strong")).to_have_text("2")
    expect(choices.locator(".learning-dial-value")).to_have_count(0)  # counts are not progress
    expect(choices.locator(".learning-entry-updated").first).to_have_attribute("datetime", "1970-01-01T00:00:40.000Z")
    choices.get_by_role("button", name="Next mix choices", exact=True).click()
    expect(choices.locator(".learning-pagination")).to_contain_text("7–8 of 8")
    expect(choices.locator(".learning-selected h4")).to_have_text("Group 7")
    choices.get_by_role("button", name=re.compile("^Chosen")).click()
    expect(choices.locator(".learning-entry-name")).to_have_text("Acoustic")
    expect(choices.locator(".learning-selected h4")).to_have_text("Acoustic")
    choices.get_by_role("button", name=re.compile("^Unchosen")).click()
    expect(choices.locator(".learning-selected h4")).to_have_text('House & "Techno"')
    choices.get_by_role("searchbox", name="Search mix choices", exact=True).fill("Group 8")
    expect(choices.locator(".learning-entry-name")).to_have_text("Group 8")
    sources = page.locator(".learning-card--sources")
    expect(sources.locator(".learning-dial-reading strong")).to_have_text("×2.00")
    expect(sources.locator(".learning-selected")).to_contain_text("Favors this source")
    expect(sources.get_by_text("No adjustment recorded", exact=True)).to_have_count(2)
    sources.get_by_role("button", name=re.compile("^Less")).click()
    expect(sources.locator(".learning-selected h4")).to_have_text("No sources below neutral")
    sources.get_by_role("button", name=re.compile("^More")).click()
    expect(sources.locator(".learning-entry-name")).to_have_text("Deep cuts")
    changes = page.locator(".learning-card--changes")
    expect(changes.locator(".learning-entry")).to_have_count(6)
    expect(changes.locator(".learning-entry-name").first).to_have_text("techno")
    expect(changes.locator(".learning-selected-context")).to_contain_text("Dislikes triggered the step")
    changes.get_by_role("button", name=re.compile("^Boosts")).click()
    expect(changes.locator(".learning-entry-name").first).to_have_text("Artist 07")
    changes.get_by_role("button", name="Next saved changes", exact=True).click()
    expect(changes.locator(".learning-pagination")).to_contain_text("7–8 of 8")
    reductions = changes.get_by_role("button", name=re.compile("^Reductions"))
    reductions.focus()
    reductions.press("Enter")
    expect(changes.locator(".learning-entry")).to_have_count(1)
    expect(changes.locator(".learning-selected")).to_contain_text("Reduction step")
    expect(changes.get_by_role("button", name="Next saved changes", exact=True)).to_be_disabled()
    changes.get_by_role("searchbox", name="Search saved changes", exact=True).fill("no matching step")
    expect(changes.locator(".learning-no-results")).to_be_visible()
    expect(changes.locator(".learning-selected h4")).to_have_text("No matching entry")
    assert all(url.endswith("/learning") for url in panels)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".learning-other").screenshot(path=str(tmp_path / f"other-learning-{width}.png"), animations="disabled")
    changes.screenshot(path=str(tmp_path / f"saved-changes-{width}.png"), animations="disabled")
    choices.get_by_role("link", name="Explore mix choice learning").click()
    expect(page.get_by_role("tab", name="Listening choices", exact=True)).to_have_attribute("aria-selected", "true")
    assert errors == []


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_learning_settings_share_compact_layout_and_preserve_edits(live_app, store, page, width, tmp_path):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#learning")
    settings = page.locator("#taste-learning-controls-settings")
    navigation = settings.get_by_role("tablist", name="Recent activity and learning settings", exact=True)
    expect(navigation.get_by_role("tab")).to_have_count(5)
    expect(settings.locator("details, .card")).to_have_count(0)
    expect(settings.get_by_role("tabpanel")).to_have_count(1)
    expect(settings.get_by_role("slider")).to_have_count(6)
    influence = navigation.get_by_role("tab", name="Recent influence", exact=True)
    influence.focus()
    page.keyboard.press("ArrowDown" if width >= 1100 else "ArrowRight")
    memory = navigation.get_by_role("tab", name="Listening memory", exact=True)
    expect(memory).to_be_focused()
    expect(settings.get_by_role("slider")).to_have_count(6)
    slider = settings.locator("#param-play_halflife_d")
    expect(slider).to_be_visible()
    slider.focus()
    expect(slider).to_be_focused()
    with page.expect_response(lambda response: response.url.endswith("/taste/param") and response.request.method == "POST"):
        page.keyboard.press("ArrowRight")
    assert rec_params.get_param(store, "play_halflife_d") == 3.5
    expect(page.locator("#taste-learning-save-status")).to_contain_text("Change saved")
    for control in settings.get_by_role("slider").all():
        assert control.bounding_box()["height"] >= 43.9  # allow subpixel layout rounding
        for described_id in control.get_attribute("aria-describedby").split():
            expect(page.locator("#" + described_id)).to_be_visible()
    bounds = settings.bounding_box()
    panel = settings.get_by_role("tabpanel").bounding_box()
    assert abs(bounds["x"] + bounds["width"] - panel["x"] - panel["width"]) <= 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    settings.screenshot(path=str(tmp_path / f"taste-learning-controls-{width}.png"), animations="disabled")
    memory.focus()
    page.keyboard.press("End")
    expect(navigation.get_by_role("tab", name="Learning rates", exact=True)).to_be_focused()
    expect(settings.get_by_role("slider")).to_have_count(6)
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    page.get_by_role("tab", name="Recent & learning", exact=True).click()
    expect(navigation.get_by_role("tab", name="Learning rates", exact=True)).to_have_attribute("aria-selected", "true")
    memory.click()
    expect(slider).to_have_value("3.5")
    page.reload()
    memory.click()
    expect(slider).to_have_value("3.5")
    with page.expect_response(lambda response: response.url.endswith("/taste/reset-param")):
        settings.get_by_role("button", name="Reset Play memory half-life (days) to default", exact=True).click()
    expect(slider).to_have_value("3")
    assert rec_params.get_param(store, "play_halflife_d") == 3
    navigation.get_by_role("tab", name="Saved preferences", exact=True).click()
    expect(settings.get_by_role("checkbox", name="Learning enabled", exact=True)).to_have_count(0)
    expect(settings.get_by_role("slider", name="Learning threshold", exact=True)).to_be_visible()
    settings.screenshot(path=str(tmp_path / f"taste-learning-saved-{width}.png"), animations="disabled")
    assert errors == []


def test_learning_readings_follow_saved_settings_without_closing_controls(live_app, store, page):
    store.rec_signals.bump_theme("genre:techno", .6, 1.0)
    page.goto(live_app + "/taste#learning")
    reading = page.locator(".learning-card--genre .learning-dial-reading strong")
    expect(reading).to_have_text("50%")
    settings = page.locator("#taste-learning-controls-settings")
    settings.get_by_role("tab", name="Saved preferences", exact=True).click()
    threshold = page.locator("#param-theme_threshold")
    threshold.evaluate("el => { el.value = '2.4'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    expect(reading).to_have_text("25%")
    expect(threshold).to_be_visible()
    expect(page.locator("#taste-learning-save-status")).to_contain_text("Change saved")
    page.get_by_role("button", name="Reset Learning threshold to default", exact=True).click()
    expect(reading).to_have_text("50%")
    expect(threshold).to_be_visible()


