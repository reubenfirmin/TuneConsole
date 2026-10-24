"""Taste topics, keyboard navigation, saved controls and deferred diagnostics."""
import re
from urllib.parse import urlsplit

import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.rec import rec_params
from yt_playlist.util.matching import identity_key

@pytest.mark.parametrize("width", [1920, 1440, 1024, 390, 320])
def test_taste_topics_show_one_group_at_a_time(store, page, monkeypatch, request, width, tmp_path):
    from yt_playlist.rec.rec_worker import RecWorker

    # Keep the synthetic layout fixture stable; startup group repair is tested separately.
    monkeypatch.setattr(RecWorker, "trigger_mode_refresh", lambda self, **kw: None)
    live_app = request.getfixturevalue("live_app")
    iid = store.identities.get_identities()[0].id
    styles = ["Warehouse techno", "Late-night jazz", "Guitar music"] + [f"Listening style {i}" for i in range(4, 15)]
    vectors = np.eye(len(styles), dtype=np.float32)
    mode_rows = []
    content_vectors = []
    recent_plays = []
    for i, style in enumerate(styles):
        genre, year = [("Techno", 1997), ("Jazz", 1964), ("Rock", 1978)][i % 3]
        rep_keys = []
        for j in range(3):
            title, artist = f"Track {i} sample {j} - The extended version", f"Artist {i} & collaborator {j}"
            thumbnail = ["/static/nag/intro.png", "/static/nag/trends.png", None][j]
            video_id = f"taste-{i}-{j}"
            track = store.tracks.upsert_track(video_id, title, artist, None, None, thumbnail=thumbnail)
            store.tracks.set_track_genre(track, genre)
            store.tracks.set_track_year(track, year)
            key = identity_key(title, artist)
            rep_keys.append(key)
            content_vectors.append((key, vectors[i].tobytes()))
            recent_plays.append((key, video_id, 1.0))
        store.history.record_history_plays(iid, 1.0, rep_keys, source_timezone="UTC")
        mode_rows.append({"mode_id": i + 1, "label": style, "families": [[genre.lower(), 1]],
                          "centroid": vectors[i], "size": 10 + i,
                          "rep_keys": rep_keys})
    store.modes.replace_modes(mode_rows, retired_ids=[], now=1.0)
    for epoch in range(12):
        store.modes.log_impressions(epoch, [("fresh", 1), ("wheelhouse", 2)], now=1.0)
    for epoch in range(100):
        store.modes.log_impressions(epoch, [("explore", 3)], now=1.0)
    for playlist in range(4):
        store.modes.log_pick(playlist, 1, now=1.0)
    store.modes.log_pick(5, 2, now=1.0)
    store.rec_model.replace_rec_content_vectors(content_vectors)
    store.history.import_play_events(iid, recent_plays[:4])
    store.rec_model.set_weight("lane:deep_cut", 1.6, now=1.0)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_app + "/taste")
    overview = page.locator("#taste-overview-panel")
    expect(overview).to_be_visible()
    expect(overview.get_by_role("heading", name="Your long-term taste", exact=True)).to_be_visible()
    expect(overview.get_by_role("heading", name="What’s changed recently?", exact=True)).to_be_visible()
    expect(overview.locator(".taste-summary")).to_have_count(3)
    expect(overview.locator("details, input")).to_have_count(0)
    expect(overview.locator(".choice-chart-row")).to_have_count(0)
    expect(overview.locator(".taste-choice-favored li")).to_have_count(1)
    expect(overview.locator(".taste-choice-less li")).to_have_count(1)
    expect(overview.locator(".taste-choice-stats dt")).to_have_text([
        "Mixes offered", "Mix selections", "Music groups chosen"])
    expect(overview.locator(".taste-choice-stats dd")).to_have_text(["124", "5", "2"])
    expect(overview.get_by_text("Want a different direction?", exact=True)).to_have_count(0)
    expect(overview.get_by_role("link", name="Choose a mix", exact=True)).to_have_count(0)
    expect(overview.locator(".taste-shift-row")).to_have_count(3)
    page.screenshot(path=str(tmp_path / f"taste-overview-{width}.png"), full_page=True)

    tabs = page.get_by_role("tablist", name="Taste Model sections", exact=True)
    expect(page.get_by_role("tablist")).to_have_count(1)
    expect(tabs.get_by_role("tab")).to_have_count(8)
    expect(tabs.get_by_role("tab").nth(1)).to_have_text("Experiment")
    positions = tabs.get_by_role("tab").evaluate_all("els => els.map(el => el.getBoundingClientRect().top)")
    assert len(set(positions)) == 1
    expect(page.locator('[role="tabpanel"] [role="tablist"]')).to_have_count(0)
    for button in tabs.get_by_role("tab").all():
        button.click()
        expect(button).to_have_attribute("aria-selected", "true")
        expect(page.locator("#" + button.get_attribute("aria-controls"))).to_be_visible()
        expect(page.get_by_role("tabpanel")).to_have_count(
            2 if button.get_attribute("id") in ("taste-experiment-tab", "taste-recommendations-tab", "taste-learning-tab") else 1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # Every topic fills the available page width, including wide desktop screens.
        dimensions = page.locator("main").evaluate("el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return {right: r.right, contentRight: r.right - parseFloat(s.paddingRight)}; }")
        assert dimensions["right"] <= width
        panel = page.locator("#" + button.get_attribute("aria-controls"))
        assert abs(panel.bounding_box()["x"] + panel.bounding_box()["width"] - dimensions["contentRight"]) <= 1

    page.get_by_role("tab", name="Listening choices", exact=True).click()
    generated = page.locator("#taste-generated-panel")
    expect(generated.locator(".generation-recipe")).to_have_count(4)
    expect(generated.locator(".generation-source")).to_have_count(3)
    expect(generated.locator(".choice-chart-row")).to_have_count(14)
    expect(generated.locator("#lane-explore")).to_have_count(0)
    expect(generated.locator('[data-source="explore"]')).to_have_count(0)
    expect(generated.get_by_role("heading", name="What your mix choices teach the model", exact=True)).to_be_visible()
    expect(generated.get_by_text("The list is run through a DJ model to produce a coherent progression.", exact=True)).to_be_visible()
    expect(generated.locator("details")).to_have_count(0)
    for slider in generated.get_by_role("slider").all():
        assert slider.bounding_box()["width"] <= 353
    page.screenshot(path=str(tmp_path / f"taste-generated-{width}.png"), full_page=True)

    page.get_by_role("tab", name="Taste profile", exact=True).click()
    profile = page.locator("#taste-profile-panel")
    expect(profile.get_by_role("heading", name="Genre families", exact=False)).to_be_visible()
    expect(profile.get_by_role("heading", name="Listening styles", exact=False)).to_be_visible()
    expect(profile.locator("details")).to_have_count(0)
    expect(profile.locator("input")).to_have_count(0)
    expect(profile.get_by_role("button")).to_have_count(0)
    expect(profile.get_by_text("Warehouse techno", exact=True)).to_be_visible()
    expect(profile.locator(".mode-row")).to_have_count(14)
    expect(profile.locator(".mode-tracks li")).to_have_count(42)
    expect(profile.locator(".mode-track-art img")).to_have_count(28)
    expect(profile.locator(".mode-track-art:not(:has(img))")).to_have_count(14)
    artwork = profile.locator(".mode-track-art img").first
    artwork.scroll_into_view_if_needed()
    page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=artwork.element_handle())
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width >= 1100:
        positions = profile.locator(".mode-row").evaluate_all("els => els.slice(0, 3).map(el => el.getBoundingClientRect().top)")
        assert len(set(positions)) == 1       # track selections use the full width, not a long sidebar
    page.screenshot(path=str(tmp_path / f"taste-profile-{width}.png"), full_page=True)
    for topic in ["Eras", "Artists"]:
        expect(profile.get_by_role("heading", name=topic, exact=False)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    recommendations = page.locator("#taste-recommendations-panel")
    expect(recommendations.locator(".taste-feature-section")).to_have_count(9)
    expect(recommendations.locator("details[open]")).to_have_count(0)
    subtabs = page.get_by_role("tablist", name="Recommendation settings", exact=True)
    expect(subtabs.get_by_role("tab")).to_have_count(4)
    expect(subtabs.get_by_role("tab", name="Intro", exact=True)).to_have_attribute("aria-selected", "true")
    expect(recommendations.locator(".taste-intro > p").first).to_contain_text("TuneConsole combines several signals")
    expect(recommendations.locator(".taste-advice")).to_have_count(0)
    expect(recommendations.get_by_role("button", name="Restore starting point", exact=True)).to_have_count(0)
    for button in subtabs.get_by_role("tab").all():
        button.click()
        expect(button).to_have_attribute("aria-selected", "true")
        expect(recommendations.locator(".taste-recommendation-panel:visible")).to_have_count(1)
        expect(page.locator("#" + button.get_attribute("aria-controls"))).to_be_visible()
        for slider in recommendations.get_by_role("slider").all():
            assert slider.bounding_box()["width"] <= 353
            assert slider.bounding_box()["height"] >= 44
        for reset in recommendations.locator(".knob-reset:visible").all():
            assert reset.bounding_box()["height"] >= 44
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("""() => ({
            width: innerWidth, scroll: document.documentElement.scrollWidth, x: scrollX,
            overflow: [...document.querySelectorAll('body *')]
              .filter(el => !el.closest('.taste-feature-nav') && el.getBoundingClientRect().right > innerWidth
                            && getComputedStyle(el).display !== 'none')
              .map(el => ({tag: el.tagName, cls: el.className, text: el.textContent.slice(0, 80),
                           width: el.getBoundingClientRect().width}))})""")
        if button.inner_text() == "Genres":
            genre_controls = recommendations.locator(".taste-genre-controls")
            assert abs(genre_controls.bounding_box()["x"] + genre_controls.bounding_box()["width"] - dimensions["contentRight"]) <= 1
    subtabs.get_by_role("tab", name="Intro", exact=True).click()
    expect(recommendations.locator("#taste-recommendation-features-panel")).to_be_hidden()
    expect(recommendations.locator("#taste-recommendation-intro-panel")).to_be_visible()
    page.screenshot(path=str(tmp_path / f"taste-recommendations-intro-{width}.png"))
    subtabs.get_by_role("tab", name="Main controls", exact=True).click()
    # Values lead the hierarchy; descriptions stay readable and available to assistive tech.
    hierarchy = recommendations.locator(".semantic-controls .knob").first.evaluate("""el => {
        const size = selector => parseFloat(getComputedStyle(el.querySelector(selector)).fontSize);
        return {value: size('output'), label: size('label'), help: size('.knob-help')};
    }""")
    assert hierarchy["value"] > hierarchy["label"] > hierarchy["help"] >= 14
    recommendations.locator(".semantic-controls").screenshot(path=str(tmp_path / f"taste-recommendations-main-{width}.png"))
    page.screenshot(path=str(tmp_path / f"taste-recommendations-{width}.png"), full_page=True)
    page.screenshot(path=str(tmp_path / f"taste-recommendations-controls-{width}.png"))
    assert errors == []


@pytest.mark.parametrize("width", [1440, 320])
def test_recommendation_subtabs_support_keyboard_deep_links_and_lazy_preview(live_app, page, width):
    requests = []
    page.on("request", lambda request: requests.append(urlsplit(request.url).path)
            if urlsplit(request.url).path in {"/taste/preview", "/taste/comparison"} else None)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_app + "/taste#recommendations")
    subtabs = page.get_by_role("tablist", name="Recommendation settings", exact=True)
    intro = subtabs.get_by_role("tab", name="Intro", exact=True)
    main = subtabs.get_by_role("tab", name="Main controls", exact=True)
    features = subtabs.get_by_role("tab", name="Settings by feature", exact=True)
    expect(page.get_by_role("heading", name="How your picks take shape", exact=True)).to_be_visible()
    expect(page.locator("#taste-checkpoint")).to_have_count(0)
    intro.focus()
    page.keyboard.press("ArrowRight")
    expect(main).to_be_focused()
    expect(main).to_have_attribute("aria-selected", "true")
    expect(page).to_have_url(live_app + "/taste#recommendations/main")
    expect(page.get_by_role("tab", name="Recommendations", exact=True)).to_have_attribute("aria-selected", "true")
    page.keyboard.press("End")
    expect(features).to_be_focused()
    expect(features).to_have_attribute("aria-selected", "true")
    expect(page.locator(".taste-recommendation-panel:visible")).to_have_count(1)
    assert requests == []

    page.reload()
    expect(features).to_have_attribute("aria-selected", "true")
    expect(features).to_be_in_viewport(ratio=1)
    expect(page.locator("#taste-recommendation-features-panel")).to_be_visible()
    features.focus()
    page.keyboard.press("Home")
    expect(intro).to_be_focused()
    expect(intro).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowLeft")
    expect(features).to_be_focused()
    expect(features).to_have_attribute("aria-selected", "true")
    page.evaluate("location.hash = 'recommendations/genres'")
    expect(page.locator("#taste-recommendation-genres-panel")).to_be_visible()
    page.evaluate("location.hash = 'recommendations/unknown'")
    expect(intro).to_have_attribute("aria-selected", "true")
    assert requests == []

    page.get_by_role("link", name="Save and compare in Experiment", exact=False).click()
    expect(page.get_by_role("heading", name="Your starting point", exact=True)).to_be_visible()
    expect(page.get_by_role("tab", name="Experiment", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page).to_have_url(live_app + "/taste#experiment")
    experiment_tabs = page.get_by_role("tablist", name="Experiment tools", exact=True)
    expect(experiment_tabs.get_by_role("tab")).to_have_count(2)
    starting = experiment_tabs.get_by_role("tab", name="Starting point", exact=True)
    preview = experiment_tabs.get_by_role("tab", name="Preview & compare", exact=True)
    expect(starting).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("heading", name="Preview & compare", exact=True)).to_be_hidden()
    assert requests == []
    starting.focus()
    page.keyboard.press("ArrowRight")
    expect(preview).to_be_focused()
    expect(preview).to_have_attribute("aria-selected", "true")
    expect(preview).to_be_in_viewport(ratio=1)
    expect(page).to_have_url(live_app + "/taste#experiment/preview")
    page.locator(".taste-preview-intro").scroll_into_view_if_needed()
    expect(page.locator("#sample-body")).not_to_contain_text("Loading")
    page.locator("#comparison-body").scroll_into_view_if_needed()
    expect(page.locator("#comparison-body")).not_to_contain_text("Loading")
    assert sorted(requests) == ["/taste/comparison", "/taste/preview"]
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    expect(intro).to_have_attribute("aria-selected", "true")
    page.get_by_role("tab", name="Experiment", exact=True).click()
    expect(page.locator("#taste-experiment-panel")).to_be_visible()
    expect(preview).to_have_attribute("aria-selected", "true")
    assert len(requests) == 2
    page.get_by_role("link", name="Starting point", exact=True).click()
    expect(starting).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("heading", name="Your starting point", exact=True)).to_be_visible()
    starting.focus()
    page.keyboard.press("ArrowLeft")
    expect(preview).to_be_focused()
    expect(preview).to_have_attribute("aria-selected", "true")
    page.reload()
    expect(preview).to_have_attribute("aria-selected", "true")
    expect(preview).to_be_in_viewport(ratio=1)
    expect(page.get_by_role("heading", name="Preview & compare", exact=True)).to_be_visible()
    page.evaluate("location.hash = 'experiment/unknown'")
    expect(starting).to_have_attribute("aria-selected", "true")
    page.goto(live_app + "/taste#recommendations/preview")
    expect(page.get_by_role("tab", name="Experiment", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page).to_have_url(live_app + "/taste#experiment/preview")
    expect(preview).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("heading", name="Preview & compare", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_feature_settings_select_features_and_preserve_saved_edits(live_app, store, page, width, tmp_path):
    # Measure the normal settings page after setup, without the initial-import banner.
    store.settings.set_setting("last_sync_at", "1")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#recommendations/features")
    features = page.locator("#taste-feature-settings")
    navigation = features.get_by_role("tablist", name="Features", exact=True)
    expect(navigation.get_by_role("tab")).to_have_count(9)
    expect(navigation.get_by_role("tab", name="Album menus", exact=True)).to_have_attribute("aria-selected", "true")
    navigation.get_by_role("tab", name="Home cards", exact=True).click()
    expect(features.locator("details")).to_have_count(0)
    expect(features.locator("#taste-feature-radio")).to_have_count(0)
    # Home keeps track length and the active setup threshold; the two inactive
    # coverage/library thresholds have been retired.
    expect(features.get_by_role("slider")).to_have_count(13)
    expect(features.locator("#param-onboard_coverage_min, #param-onboard_library_min")).to_have_count(0)
    expect(features.locator("#param-max_track_minutes")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    home = features.locator("#taste-feature-home")
    home_tab = navigation.get_by_role("tab", name="Home cards", exact=True)
    expect(home_tab).to_have_attribute("aria-selected", "true")
    expect(features.get_by_role("tabpanel")).to_have_count(1)
    expect(home.get_by_role("heading", name="Comfort listening", exact=True)).to_be_visible()
    expect(home.get_by_role("heading", name="Cards & variety", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / f"taste-features-{width}.png"), animations="disabled")
    if width == 1440:
        density = home.locator(".knob").evaluate_all("""rows => ({
            visible: rows.filter(row => { const r = row.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }).length,
            heights: rows.map(row => row.getBoundingClientRect().height)
        })""")
        assert density["visible"] >= 8, density
        assert sorted(density["heights"])[len(density["heights"]) // 2] <= 85
    for slider in home.get_by_role("slider").all():
        assert slider.bounding_box()["height"] >= 44
        for described_id in slider.get_attribute("aria-describedby").split():
            expect(page.locator("#" + described_id)).to_be_visible()
    # Content fills the pane; only the physical slider rail has a narrower width.
    pane = page.locator("#taste-recommendation-features-panel").bounding_box()
    bounds = home.bounding_box()
    assert abs(bounds["x"] + bounds["width"] - pane["x"] - pane["width"]) <= 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    home.screenshot(path=str(tmp_path / f"taste-features-home-{width}.png"), animations="disabled")

    slider = home.locator("#param-erosion_view_cap")
    slider.focus()
    with page.expect_response(lambda response: response.url.endswith("/taste/param") and response.request.method == "POST"):
        page.keyboard.press("ArrowRight")
    assert rec_params.get_param(store, "erosion_view_cap") == 4
    expect(home.locator("#out-param-erosion_view_cap")).to_have_text("4")
    home_tab.focus()
    page.keyboard.press("ArrowDown" if width >= 1100 else "ArrowRight")
    styles_tab = navigation.get_by_role("tab", name="Listening-style grouping", exact=True)
    expect(styles_tab).to_be_focused()
    expect(styles_tab).to_have_attribute("aria-selected", "true")
    expect(features.locator("#taste-feature-styles")).to_be_visible()
    expect(home.locator("#param-modes_k")).to_have_count(0)
    expect(features.locator("#taste-feature-styles #param-modes_k")).to_be_visible()
    expect(features.get_by_role("slider")).to_have_count(3)
    page.keyboard.press("ArrowDown" if width >= 1100 else "ArrowRight")
    discovery_tab = navigation.get_by_role("tab", name="Finding new music", exact=True)
    expect(discovery_tab).to_be_focused()
    expect(discovery_tab).to_have_attribute("aria-selected", "true")
    expect(slider).to_be_hidden()
    expect(features.get_by_role("slider")).to_have_count(3)
    expect(features.locator("#param-palette_absence_penalty")).to_have_count(0)
    expect(features.get_by_text("Favor familiar genres", exact=True)).to_have_count(0)
    page.keyboard.press("End")
    expect(navigation.get_by_role("tab", name="Generated playlist cleanup", exact=True)).to_be_focused()
    expect(features.get_by_role("slider")).to_have_count(1)
    page.keyboard.press("Home")
    expect(navigation.get_by_role("tab", name="Album menus", exact=True)).to_be_focused()
    page.keyboard.press("ArrowDown" if width >= 1100 else "ArrowRight")
    expect(home_tab).to_be_focused()
    expect(slider).to_have_value("4")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.reload()
    home_tab.click()
    expect(slider).to_have_value("4")
    with page.expect_response(lambda response: response.url.endswith("/taste/reset-param")):
        home.get_by_role("button", name="Reset Home visits before suggestions refresh to default", exact=True).click()
    expect(slider).to_have_value("3")
    assert rec_params.get_param(store, "erosion_view_cap") == 3


@pytest.mark.parametrize("query", ["", "?radio=true", "?radio=1"])
def test_radio_settings_are_absent_even_with_the_old_opt_in(live_app, page, query):
    page.goto(live_app + "/taste" + query + "#recommendations/features")
    page.get_by_role("tab", name="Home cards", exact=True).click()
    expect(page.locator("#taste-feature-home")).to_be_visible()
    expect(page.get_by_role("tab", name="On air", exact=True)).to_have_count(0)
    expect(page.locator("#taste-feature-radio, [data-param^=radio_]")).to_have_count(0)


def test_recommendation_control_contrast_and_forced_colors(live_app, page, tmp_path):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(live_app + "/taste#recommendations/main")
    expect(page.locator("#param-breadth_bias")).to_be_visible()
    contrast = page.locator(".taste-recommendations").evaluate("""root => {
        const ctx = document.createElement('canvas').getContext('2d');
        function rgba(color) {
            ctx.clearRect(0, 0, 1, 1);
            ctx.fillStyle = color;
            ctx.fillRect(0, 0, 1, 1);
            return [...ctx.getImageData(0, 0, 1, 1).data];
        }
        function luminance(rgb) {
            const [r, g, b] = rgb.slice(0, 3).map(v => {
                v /= 255;
                return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
            });
            return .2126 * r + .7152 * g + .0722 * b;
        }
        function ratio(a, b) {
            const x = luminance(a), y = luminance(b);
            return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
        }
        function background(el) {
            for(let parent = el; parent; parent = parent.parentElement) {
                const color = rgba(getComputedStyle(parent).backgroundColor);
                if(color[3] === 255) return color;
            }
            return rgba(getComputedStyle(document.documentElement).getPropertyValue('--canvas'));
        }
        const texts = [...root.querySelectorAll('.knob label, .knob output, .knob-help, .knob-origin, .knob-scale span:not(.knob-default-marker), .knob-reset, .taste-feature-nav > button, .taste-feature-note, .taste-feature-count, .taste-feature-group h3')];
        const rails = [...root.querySelectorAll('.knob input[type=range]')];
        return {
            text: Math.min(...texts.map(el => ratio(rgba(getComputedStyle(el).color), background(el)))),
            rail: Math.min(...rails.map(el => ratio(rgba(getComputedStyle(el).getPropertyValue('--faint')), background(el))))
        };
    }""")
    assert contrast["text"] >= 4.5
    assert contrast["rail"] >= 3
    subtabs = page.get_by_role("tablist", name="Recommendation settings", exact=True)
    subtabs.get_by_role("tab", name="Settings by feature", exact=True).click()
    page.locator(".taste-feature-section .knob").first.screenshot(path=str(tmp_path / "feature-setting-row.png"))
    subtabs.get_by_role("tab", name="Main controls", exact=True).click()
    page.emulate_media(forced_colors="active")
    slider = page.get_by_role("slider", name="Genre variety", exact=True)
    expect(slider).to_be_visible()
    assert slider.evaluate("el => getComputedStyle(el).appearance") == "auto"


@pytest.mark.parametrize("name,default,default_percent", [
    ("play_transient_w", 0.30, "15%"),
    ("like_transient_w", 0.45, "22.5%"),
    ("audio_transient_w", 0.30, "15%"),
])
def test_recent_influence_percentages_save_and_reset(live_app, store, page, name, default, default_percent):
    # Existing model-unit values render correctly, persist after edits, and reset without migration.
    rec_params.set_param(store, name, 1.0)
    page.goto(live_app + "/taste#recommendations/main")
    slider = page.locator(f"#param-{name}")
    output = page.locator(f"#out-param-{name}")
    expect(output).to_have_text("50%")
    expect(slider).to_have_attribute("aria-valuetext", "50%")
    expect(page.locator(f"#default-param-{name}")).to_have_text(f"Default {default_percent}")
    help_text = page.locator(f"#help-param-{name}")
    expect(help_text).to_be_visible()
    expect(slider).to_have_attribute("aria-describedby", f"help-param-{name} default-param-{name}")
    slider.focus()
    assert slider.evaluate("el => getComputedStyle(el).outlineWidth") == "3px"
    with page.expect_response(lambda r: r.url.endswith("/taste/param") and r.status == 200):
        page.keyboard.press("ArrowRight")
    expect(output).to_have_text("52.5%")
    expect(slider).to_have_attribute("aria-valuetext", "52.5%")
    assert rec_params.get_param(store, name) == 1.05

    for model_value, percentage in [(0, "0%"), (2, "100%"), (0.65, "32.5%")]:
        with page.expect_response(lambda r: r.url.endswith("/taste/param") and r.status == 200):
            slider.evaluate("(el, value) => { el.value = value; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }", str(model_value))
        expect(output).to_have_text(percentage)
        expect(slider).to_have_attribute("aria-valuetext", percentage)
        assert rec_params.get_param(store, name) == model_value
        page.reload()
        expect(output).to_have_text(percentage)
        expect(slider).to_have_attribute("aria-valuetext", percentage)

    with page.expect_response(lambda r: r.url.endswith("/taste/reset-param") and r.status == 200):
        slider.locator("..").get_by_role("button", name="Reset", exact=False).click()
    expect(output).to_have_text(default_percent)
    expect(slider).to_have_attribute("aria-valuetext", default_percent)
    assert rec_params.get_param(store, name) == default
    page.reload()
    expect(output).to_have_text(default_percent)


def test_taste_controls_survive_topic_switches_and_reload(live_app, store, page):
    iid = store.identities.get_identities()[0].id
    track = store.tracks.upsert_track("genre-test", "Genre test", "Test artist", None, None)
    store.tracks.set_track_genre(track, "Techno")
    store.history.record_history_plays(iid, 1.0, ["genre test|test artist"], source_timezone="UTC")
    page.goto(live_app + "/taste#experiment/preview")
    page.locator(".taste-preview-intro").scroll_into_view_if_needed()
    expect(page.locator("#sample-body")).not_to_contain_text("Loading")
    page.get_by_role("link", name="Main controls", exact=True).click()
    slider = page.locator("#param-breadth_bias")
    expect(slider).to_be_visible()
    with page.expect_response(lambda r: r.url.endswith("/taste/param") and r.status == 200):
        slider.evaluate("el => { el.value = '0.5'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
    assert rec_params.get_param(store, "breadth_bias") == 0.5
    expect(page.locator("#out-param-breadth_bias")).to_have_text("0.50")
    expect(page.locator("#foryou-sample")).to_have_class(re.compile(r"\bstale\b"))
    expect(page.locator("#foryou-sample .sample-note")).to_contain_text("Settings changed")
    subtabs = page.get_by_role("tablist", name="Recommendation settings", exact=True)
    page.get_by_role("tab", name="Experiment", exact=True).click()
    page.get_by_role("button", name="Refresh sample", exact=True).click()
    expect(page.locator("#sample-body")).not_to_contain_text("Loading")
    expect(page.locator("#foryou-sample")).not_to_have_class(re.compile(r"\bstale\b"))
    page.get_by_role("tab", name="Overview", exact=True).click()
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    subtabs.get_by_role("tab", name="Main controls", exact=True).click()
    expect(slider).to_have_value("0.5")

    page.get_by_role("tab", name="Listening choices", exact=True).click()
    source = page.locator("#lane-deep_cut")
    expect(source).to_be_visible()
    with page.expect_response(lambda r: r.url.endswith("/taste/weight") and r.status == 200):
        source.evaluate("el => { el.value = '1.8'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
    expect(page.locator("#out-lane-deep_cut")).to_have_text("×1.80")
    expect(page.locator("#taste-generated-save-status")).to_contain_text("Change saved")
    page.reload()
    expect(source).to_have_value("1.8")
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    subtabs.get_by_role("tab", name="Genres", exact=True).click()

    genre = page.get_by_role("slider", name="techno", exact=False)
    expect(genre).to_be_visible()
    with page.expect_response(lambda r: r.url.endswith("/taste/weight") and r.status == 200):
        genre.evaluate("el => { el.value = '0.8'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
    assert store.rec_model.get_weights(now=1.0)["genre:techno"] == 0.8
    expect(genre.locator("..").locator(".knob-origin")).to_have_text("Saved ×0.80 · just now")
    expect(page.locator("#taste-recommendations-panel")).not_to_contain_text("For You")
    page.reload()
    expect(genre).to_be_visible()
    subtabs.get_by_role("tab", name="Main controls", exact=True).click()
    expect(slider).to_be_visible()
    expect(slider).to_have_value("0.5")

    # A reset refreshes the whole page; its topic must remain open.
    page.get_by_role("tab", name="Experiment", exact=True).click()
    page.get_by_role("tab", name="Starting point", exact=True).click()
    page.get_by_role("button", name="Reset all settings", exact=True).click()
    with page.expect_navigation():
        page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(page.get_by_role("tab", name="Experiment", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("heading", name="Your starting point", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    subtabs.get_by_role("tab", name="Main controls", exact=True).click()
    expect(slider).to_have_value(f'{rec_params.PARAMS_BY_NAME["breadth_bias"].default:g}')
    assert rec_params.get_param(store, "breadth_bias") == 0

    # The primary bar supports the keyboard independently of recommendation subtabs.
    page.set_viewport_size({"width": 390, "height": 844})
    topics = page.get_by_role("tablist", name="Taste Model sections", exact=True)
    topics.get_by_role("tab", name="Recommendations", exact=True).focus()
    page.keyboard.press("End")
    feedback_tab = topics.get_by_role("tab", name="Feedback", exact=True)
    expect(feedback_tab).to_be_focused()
    expect(feedback_tab).to_have_attribute("aria-selected", "true")
    page.reload()
    expect(feedback_tab).to_be_in_viewport()
    expect(page.locator("#taste-feedback-panel")).to_be_visible()
    page.evaluate("location.hash = 'profile'")
    expect(page.locator("#taste-profile-panel")).to_be_visible()


def test_taste_recovery_restores_preexisting_settings_after_reload(live_app, store, page):
    rec_params.set_param(store, "breadth_bias", -0.25)
    store.rec_model.set_weight("genre:rock", 0, lo=0, now=-100)
    page.goto(live_app + "/taste#experiment")
    slider = page.locator("#param-breadth_bias")
    restore = page.get_by_role("button", name="Restore starting point", exact=True, include_hidden=True)
    expect(restore).to_be_disabled()
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    subtabs = page.get_by_role("tablist", name="Recommendation settings", exact=True)
    subtabs.get_by_role("tab", name="Main controls", exact=True).click()
    with page.expect_response(lambda r: r.url.endswith("/taste/param") and r.status == 200):
        slider.evaluate("el => { el.value = '0.5'; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }")
    expect(restore).to_be_enabled()
    expect(page.locator("#taste-save-status")).to_contain_text("Change saved")
    page.reload()
    expect(slider).to_have_value("0.5")
    page.get_by_role("tab", name="Experiment", exact=True).click()
    restore.click()
    with page.expect_navigation():
        page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(page.get_by_role("tab", name="Experiment", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("heading", name="Your starting point", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    subtabs.get_by_role("tab", name="Main controls", exact=True).click()
    expect(slider).to_have_value("-0.25")
    assert store.rec_model.weight_records()["genre:rock"]["weight"] == 0


@pytest.mark.parametrize("height", [720, 1200])
def test_taste_loads_diagnostics_only_when_opened(live_app, page, height):
    paths = {"/taste/viz/engine", "/taste/viz/recent-sound", "/taste/viz/playlist-influence",
             "/taste/recall", "/taste/preview", "/taste/comparison"}
    requests = []
    page.set_viewport_size({"width": 1280, "height": height})
    page.on("request", lambda request: requests.append(urlsplit(request.url).path)
            if urlsplit(request.url).path in paths else None)
    page.goto(live_app + "/taste")
    expect(page.locator("#taste-overview-panel")).to_be_visible()
    assert requests == []
    page.get_by_role("tab", name="Model health", exact=True).click()
    expect(page.locator("#taste-health-panel").get_by_text("Learning method", exact=True)).to_be_visible()
    # The intersect response replaces this wrapper. Scroll in one browser task so
    # Playwright cannot wait for layout stability on a node the response just removed.
    page.locator("#model-health-checks").evaluate(
        "el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    expect(page.locator("#model-status")).to_contain_text("Future listening")
    assert requests == ["/taste/recall"]
    page.get_by_role("tab", name="Overview", exact=True).click()
    page.get_by_role("tab", name="Model health", exact=True).click()
    expect(page.locator("#taste-health-panel").get_by_text("Learning method", exact=True)).to_be_visible()
    assert requests == ["/taste/recall"]

    page.get_by_role("tab", name="Taste profile", exact=True).click()
    influence = page.locator("#taste-playlist-influence")
    influence.scroll_into_view_if_needed()
    expect(influence).to_contain_text("Build the model to see the interests")
    assert requests == ["/taste/recall", "/taste/viz/playlist-influence"]
    page.get_by_role("tab", name="Recent & learning", exact=True).click()
    sound = page.locator("#taste-recent-sound")
    sound.scroll_into_view_if_needed()
    expect(sound).to_contain_text("Not enough recent listening to compare sounds yet")
    assert requests == ["/taste/recall", "/taste/viz/playlist-influence", "/taste/viz/recent-sound"]
    page.get_by_role("tab", name="Taste profile", exact=True).click()
    influence.scroll_into_view_if_needed()
    expect(influence).to_contain_text("Build the model to see the interests")
    assert requests.count("/taste/viz/playlist-influence") == 1


def test_taste_loads_only_selected_panel_and_keeps_it_on_return(live_app, page):
    requests = []
    page.on("request", lambda request: requests.append(urlsplit(request.url).path)
            if "/taste/panels/" in request.url else None)
    page.goto(live_app + "/taste#recommendations")
    expect(page.get_by_role("tab", name="Intro", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator("#param-breadth_bias")).to_be_hidden()
    assert requests == ["/taste/panels/recommendations"]
    page.get_by_role("tab", name="Overview", exact=True).click()
    expect(page.get_by_role("heading", name="Your long-term taste", exact=True)).to_be_visible()
    assert requests == ["/taste/panels/recommendations", "/taste/panels/overview"]
    page.get_by_role("tab", name="Recommendations", exact=True).click()
    expect(page.locator("#taste-recommendation-intro-panel")).to_be_visible()
    assert len(requests) == 2


def test_taste_panel_failure_can_be_retried(live_app, page):
    page.route("**/taste/panels/overview", lambda route: route.fulfill(status=503, body="Unavailable"))
    page.goto(live_app + "/taste")
    expect(page.get_by_role("status").filter(has_text="Could not load Overview.")).to_be_visible()
    page.unroute("**/taste/panels/overview")
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_role("heading", name="Your long-term taste", exact=True)).to_be_visible()
