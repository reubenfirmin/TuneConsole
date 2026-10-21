"""Taste topics stay focused, preserve edits, and defer hidden diagnostics."""
import json
import re
from urllib.parse import urlsplit

import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.rec import autotune_run, eval_recs, rec_params
from yt_playlist.util.matching import identity_key


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_preview_and_compare_show_artwork_play_tracks_and_keep_baseline(live_app, store, page, monkeypatch, width, tmp_path):
    from yt_playlist.rec import recommend

    title = 'A song with a "long" title & another verse'
    trace = {"durable": .4, "transient_delta": .1, "session_delta": 0,
             "audio_delta": 0, "axes": {}, "contexts": [{"name": "Night drive"}]}
    sample = [recommend.ForYouItem(title, "Artist & guest", "An album", "preview-song",
                                  "/static/nag/intro.png", 0, "In your taste neighbourhood",
                                  key="song|artist", lane="neighbourhood", trace=trace),
              recommend.ForYouItem("Missing artwork", "Another artist", "", "missing-art",
                                  "/static/missing-art.jpg", 0, "A deep cut",
                                  key="missing|artist", lane="deep_cut"),
              recommend.ForYouItem("Unresolved song", "Unknown artist", "", None,
                                  None, 0, "Playlist neighbor", key="unknown|artist", lane="rotation")]
    monkeypatch.setattr(recommend, "taste_sample", lambda *a, **kw: sample)
    comparison_calls = []

    def compare(_store, _now, keys=None, limit=24):
        comparison_calls.append(keys)
        changed = rec_params.get_param(store, "breadth_bias") != 0
        first = {"key": "song|artist", "title": title, "artist": "Artist & guest",
                 "album": "An album", "video_id": "preview-song", "thumbnail": "/static/nag/intro.png",
                 "rank": 2 if changed else 1, "in_cohort": True, "score": .8,
                 "trace": trace, "lane": "neighbourhood", "lane_weight": 1,
                 "eligible": True, "eligibility_reason": None}
        old = {**first, "key": "old|artist", "title": "Former pick", "video_id": "former-pick",
               "rank": None if changed else 2, "in_cohort": not changed, "lane": "deep_cut"}
        if not changed:
            return [first, old]
        new = {**first, "key": "new|artist", "title": "New pick", "video_id": "new-pick", "rank": 1}
        return [new, first, old]

    monkeypatch.setattr(recommend, "taste_comparison", compare)
    play_requests = []
    page.route("**/play", lambda route: (play_requests.append(route.request.post_data_json),
                                        route.fulfill(json={"ok": True})))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#experiment/preview")
    page.locator(".taste-preview-intro").scroll_into_view_if_needed()
    sample_table = page.get_by_role("table", name="Sample suggestions", exact=True)
    expect(sample_table.get_by_role("row")).to_have_count(4)
    expect(sample_table.locator(".taste-track-art img")).to_have_count(1)
    expect(sample_table.get_by_text("Playback unavailable", exact=True)).to_be_visible()
    play = sample_table.get_by_role("link", name=f"Play {title} by Artist & guest on YouTube Music", exact=True)
    assert play.bounding_box()["width"] >= 44
    play.focus()
    with page.expect_response(lambda response: urlsplit(response.url).path == "/play"):
        play.press("Enter")
    assert play_requests == [{"url": "https://music.youtube.com/watch?v=preview-song"}]
    expect(sample_table.get_by_role("link", name=title, exact=True)).to_have_attribute("data-yt-info", "")

    page.locator("#comparison-body").scroll_into_view_if_needed()
    expect(page.locator("#comparison-body")).not_to_contain_text("Loading")
    page.get_by_role("button", name="Capture current picks", exact=True).click()
    expect(page.locator("#comparison-body [role=status]")).to_contain_text("Current picks captured")
    before = store.get_setting("taste_comparison_baseline_v2")
    expect(page.get_by_role("table", name="Before and after suggestions").get_by_role("row")).to_have_count(3)
    page.get_by_role("tab", name="Starting point", exact=True).click()
    expect(page.get_by_role("heading", name="Your starting point", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Preview & compare", exact=True).click()
    expect(page.get_by_role("table", name="Before and after suggestions").get_by_role("row")).to_have_count(3)
    assert store.get_setting("taste_comparison_baseline_v2") == before
    page.get_by_role("link", name="Main controls", exact=True).click()
    slider = page.locator("#param-breadth_bias")
    slider.evaluate("el => { el.value = '.5'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    expect(page.locator("#taste-save-status")).to_contain_text("Change saved")
    page.get_by_role("tab", name="Experiment", exact=True).click()
    expect(page.locator(".sample-note")).to_contain_text("Settings changed")
    page.get_by_role("button", name="Compare changes", exact=True).click()
    table = page.get_by_role("table", name="Before and after suggestions")
    expect(table.get_by_role("row")).to_have_count(4)
    expect(table.locator(".taste-rank-change")).to_have_text(["NEW", "Down 1", "OUT"])
    expect(table.locator(".taste-track-rank")).to_have_text(["Not captured", "#1", "#1", "#2", "#2", "Outside top 24"])
    assert store.get_setting("taste_comparison_baseline_v2") == before
    assert comparison_calls[-1] == ["song|artist", "old|artist"]
    with page.expect_response(lambda response: urlsplit(response.url).path == "/play"):
        table.get_by_role("link", name="Play Former pick by Artist & guest on YouTube Music", exact=True).click()
    assert play_requests[-1] == {"url": "https://music.youtube.com/watch?v=former-pick"}
    assert page.url.endswith("#experiment/preview")
    assert len(page.context.pages) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".taste-comparison").screenshot(path=str(tmp_path / f"taste-comparison-{width}.png"))
    page.get_by_role("button", name="Refresh sample", exact=True).click()
    expect(page.locator(".sample-note")).to_contain_text("Sample updated")
    page.locator(".foryou-sample").screenshot(path=str(tmp_path / f"taste-preview-{width}.png"))
    page.get_by_role("tablist", name="Experiment tools", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"taste-experiment-subtabs-{width}.png"))
    assert errors == []


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_learning_cards_search_page_and_explain_real_progress(live_app, store, page, width, tmp_path):
    for i in range(15):
        store.bump_theme(f"artist:Artist {i:02}", .3 + i * .03, 1.0)
    store.bump_theme('artist:An artist’s "long" name & AC:DC', -.9, 1.0)
    store.bump_theme("genre:other:alternative metal", .6, 1.0)
    store.bump_theme("era:1990", .9, 1.0)
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
        store.bump_theme(f"artist:Positive {i:03}", .9, now + 1000 + i)
    for i in range(7):
        store.bump_theme(f"artist:Negative {i:02}", -.12 - i * .012, now + 100 + i)
    store.bump_theme("artist:Negative priority", -.06, now + 200)
    store.bump_theme("artist:Balanced", 0, 0)
    store.bump_theme("genre:jazz", .3, 1.0)
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
    store.set_weight("lane:deep_cut", 2, now=1)
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
    expect(page.get_by_role("tab", name="Generated playlists", exact=True)).to_have_attribute("aria-selected", "true")
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
    expect(settings.get_by_role("slider")).to_have_count(7)
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
    store.bump_theme("genre:techno", .6, 1.0)
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


def test_overview_updates_itself_when_background_genre_repair_finishes(store, page, monkeypatch, request):
    from yt_playlist.rec import embed
    from yt_playlist.rec.rec_worker import RecWorker
    repairing = {"busy": True}
    monkeypatch.setattr(RecWorker, "trigger_mode_refresh", lambda self, **kw: None)
    monkeypatch.setattr(RecWorker, "busy", property(lambda self: repairing["busy"]))
    live_app = request.getfixturevalue("live_app")
    iid = store.get_identities()[0].id
    store.set_setting("rec_content_model", json.dumps({"cat": {"fam:techno": 0}, "ncat": 1, "cont": []}))
    keys = []
    for i in range(3):
        tid = store.upsert_track(f"repair-{i}", f"Repair {i}", "Artist", None, None)
        store.set_track_genre(tid, "Techno")
        keys.append(f"repair {i}|artist")
    store.replace_rec_content_vectors([(key, np.ones(1, dtype=np.float32).tobytes()) for key in keys])
    store.import_play_events(iid, [(key, f"repair-{i}", 1.0) for i, key in enumerate(keys)])
    mode = {"mode_id": 1, "label": "Techno", "families": [["techno", 3]],
            "centroid": np.ones(1, dtype=np.float32), "size": 3, "rep_keys": keys, "space": "old"}
    store.modes.replace_modes([mode], retired_ids=[], now=1.0)
    page.goto(live_app + "/taste")
    overview = page.locator("#taste-overview-panel")
    expect(overview.get_by_role("status")).to_contain_text("Updating your music groups automatically")
    mode["space"] = embed.content_space_id(store)
    store.modes.replace_modes([mode], retired_ids=[], now=2.0)
    repairing["busy"] = False
    expect(overview.locator(".taste-shift-row")).to_have_count(1, timeout=7000)
    expect(overview.locator('[hx-trigger="every 3s"]')).to_have_count(0)
    expect(overview.locator(".taste-status-line")).to_have_count(1)
    expect(overview.locator(".taste-summary")).to_have_count(3)
    expect(page.locator("#taste-profile-panel .taste-panel-content")).to_contain_text("Loading taste profile")


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_playlist_influence_shows_the_whole_blend_and_links_selected_rows(live_app, store, page, width, tmp_path):
    iid = store.get_identities()[0].id
    titles = ['Night drives / broken beats & "other" late discoveries',
              "Sunday slow / piano, soul and a little rain"]
    counts = [34, 22, 14, 10, 6, 5, 4, 2, 2, 1, 0]
    vectors, pids = [], []
    for i, count in enumerate(counts):
        title = titles[i] if i < len(titles) else f"Listening room {i + 1}"
        tracks = []
        for j, genre in enumerate(("Techno" if i % 2 == 0 else "Folk", "Jazz")):
            tid = store.upsert_track(f"influence-{i}-{j}", f"Blend {i} {j}", "Artist", None, None)
            store.set_track_genre(tid, genre)
            tracks.append(tid)
            vectors.append((f"blend {i} {j}|artist", np.array([1, i + 1], dtype=np.float32).tobytes()))
        pid = store.upsert_playlist(iid, f"influence-{i}", title, 2, f"influence-{i}", 0)
        store.set_playlist_tracks(pid, tracks)
        pids.append(pid)
        for play in range(count):
            store.add_history_snapshot(iid, i * 100 + play, [f"blend {i} 0|artist"])
    store.replace_rec_vectors(vectors)
    store.set_setting("rec_dim", "2")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#profile")
    influence = page.locator("#taste-playlist-influence")
    influence.scroll_into_view_if_needed()
    rows = influence.locator(".playlist-influence-row")
    expect(rows).to_have_count(9)
    expect(influence.locator(".playlist-blend-segment")).to_have_count(9)
    expect(influence.get_by_text("10 playlists shape this match", exact=True)).to_be_visible()
    expect(influence.locator(".playlist-blend-caption")).to_contain_text("70% comes from your top 3")
    expect(rows.first.locator(".playlist-influence-title")).to_have_text(titles[0])
    expect(rows.first.locator(".playlist-influence-plays")).to_contain_text("34")
    expect(rows.first.locator(".playlist-influence-genres")).to_contain_text("techno")
    expect(rows.last.locator(".playlist-influence-title")).to_have_text("Other 2 playlists")
    expect(rows.last.locator(".playlist-influence-share")).to_contain_text("3%")
    expect(influence.locator(".playlist-influence-footnote")).to_contain_text("1 playlist has no listening weight yet")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    influence.locator("..").screenshot(path=str(tmp_path / f"playlist-blend-{width}.png"))

    ring = influence.locator(".playlist-blend-ring")
    ring.scroll_into_view_if_needed()
    bounds = ring.bounding_box()
    page.mouse.move(bounds["x"] + bounds["width"] * .75, bounds["y"] + bounds["height"] * .25)
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("34%")
    page.mouse.move(0, 0)
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("100%")
    rows.first.click()
    page.mouse.move(0, 0)
    expect(rows.first).to_have_attribute("aria-pressed", "true")
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("34%")
    expect(influence.locator(".playlist-blend-segment.is-muted")).to_have_count(8)
    expect(influence.get_by_role("link", name="Open playlist", exact=True).filter(visible=True)).to_have_attribute("href", f"/playlist/{pids[0]}")
    influence.locator("..").screenshot(path=str(tmp_path / f"playlist-blend-selected-{width}.png"))
    rows.nth(1).focus()
    rows.nth(1).press("Enter")
    expect(rows.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("22%")
    rows.nth(1).press("Escape")
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("100%")
    expect(influence.locator(".playlist-blend-segment.is-muted")).to_have_count(0)
    assert errors == []


@pytest.mark.parametrize("width", [1920, 1440, 1024, 390, 320])
def test_model_health_groups_results_and_explains_tuning(live_app, store, page, monkeypatch, width, tmp_path):
    iid = store.get_identities()[0].id
    tracks, vectors = [], []
    for i in range(24):
        tid = store.upsert_track(f"health-{i}", f"Track {i}", f"Artist {i % 4}", "Album", None)
        tracks.append(tid)
        store.set_track_genre(tid, "Jazz" if i % 2 else "Techno")
        vectors.append((f"track {i}|artist {i % 4}", np.eye(4, dtype=np.float32)[i % 4].tobytes()))
    store.set_playlist_tracks(store.upsert_playlist(iid, "health-mix", "Weekend mix", 24, "health", 0), tracks)
    store.replace_rec_vectors(vectors)
    store.set_setting("rec_dim", "4")
    store.set_setting("rec_embed_method", "svd")
    store.add_history_snapshot(iid, 0, [key for key, _ in vectors])
    monkeypatch.setattr(eval_recs, "recall_at_k", lambda *args, **kwargs:
                        {"recall_at_k": .68, "k": 20, "trials": 42, "lift": 2.4})
    monkeypatch.setattr(eval_recs, "rolling_temporal_recall", lambda *args, **kwargs:
                        {"recall": .34, "k": 20, "trials": 120, "viable_windows": 2,
                         "requested_windows": 3, "generated_excluded": 8})
    monkeypatch.setattr(eval_recs, "projection_recall", lambda *args, **kwargs:
                        {"recall": .52, "k": 20, "trials": 80, "breakdown": {"by_coverage": {
                            "genre": {"recall": .40, "trials": 40},
                            "genre+year+audio": {"recall": .64, "trials": 40}}}})
    monkeypatch.setattr(store, "recommendation_scorecard", lambda *args, **kwargs:
                        {"impressions": 80, "played": 16, "acceptance_rate": .20,
                         "organic": 10, "generated": 8, "powered": False, "min_impressions": 100,
                         "open": 8, "start": 10, "save": 4, "promotion": 1, "conversion": 2,
                         "cold_impressions": 8, "unique_artists": 12, "unique_genres": 3,
                         "by_ownership": {"cold": {"played": 2, "impressions": 8, "powered": False}},
                         "by_metadata": {"genre": {"missing": {"played": 1, "impressions": 5,
                                                                  "powered": False}}}})
    monkeypatch.setattr(store, "graduation_log_counts", lambda: {"play": 38, "like": 8, "slider": 3})
    store.set_setting(autotune_run.RESULT_SETTING, json.dumps({
        "ran_at": 1_792_512_000,
        "winner": {"method": "svd", "dim": 64, "recall": .34},
        "previous": {"method": "svd", "dim": 48, "recall": .29},
        "grid": [{"method": "svd", "dim": 64, "recall": .34},
                 {"method": "svd", "dim": 48, "recall": .29},
                 {"method": "item2vec", "dim": 64, "recall": 0, "failed": True}],
        "metric": "causal_temporal_recall", "in_sample": False,
        "recs": {"compared": 15,
                 "added": [{"title": "A new track with an extended title", "artist": "New artist"}],
                 "dropped": [{"title": "A previous suggestion", "artist": "Another artist"}]}}))
    run = {"ran_at": 1_792_512_000, "status": "applied", "reason": "Improved predictions across multiple test periods.",
           "previous": {"method": "svd", "dim": 48}, "selected": {"method": "svd", "dim": 64},
           "comparison": {"eligible": True, "trials": 120, "windows": 3, "previous_score": .29, "score": .34},
           "grid": [{"method": "svd", "dim": 64, "eligible": True, "score": .34, "reason": "Clear improvement"},
                    {"method": "item2vec", "dim": 64, "eligible": False, "reason": "Incomplete test coverage"}],
           "followup": {"trials": 80, "previous_hits": 20, "selected_hits": 28, "previous_score": .25, "score": .35,
                        "forecasts": 3, "updated_at": 1_792_512_000 + 7 * 86400,
                        "points": [{"at": 1_792_512_000 + 2 * 86400, "trials": 20, "previous_score": .3, "score": .4},
                                   {"at": 1_792_512_000 + 7 * 86400, "trials": 80, "previous_score": .25, "score": .35}]}}
    run_id = store.rec.model.start_selection(run["ran_at"], run)
    store.set_setting("rec_selection_active_run", str(run_id))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#health")
    health = page.locator("#taste-health-panel")
    expect(health.get_by_role("heading", name="How your model is doing", exact=True)).to_be_visible()
    facts = health.locator(".model-health-facts")
    expect(facts.locator("strong").nth(0)).to_have_text("24")
    expect(facts.locator("strong").nth(1)).to_have_text("SVD")
    expect(facts.locator("strong").nth(2)).to_have_text("4 dimensions")
    expect(facts.locator("xpath=ancestor::details")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    facts.screenshot(path=str(tmp_path / f"model-health-kpis-{width}.png"))
    health.locator("#model-health-checks").evaluate("el => el.scrollIntoView({block: 'start'})")
    for heading in ("How well it predicts", "What happens when you listen", "Automatic model selection"):
        expect(health.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    expect(health.locator(".model-health-tests .model-health-reading")).to_have_text(["34%", "68%", "52%"])
    expect(health.get_by_text("Early result:", exact=False)).to_be_visible()
    expect(health.get_by_text("Saved slider settings", exact=True)).to_be_visible()
    results = health.get_by_role("table", name="Model prediction comparison", exact=True)
    expect(results.get_by_role("row", name="Difference", exact=False)).to_contain_text("+5.0 pts")
    expect(results.get_by_role("row", name="Difference", exact=False)).to_contain_text("+10.0 pts")
    expect(health.locator(".selection-chart")).to_be_visible()
    expect(health.locator("#autotune-status details")).to_have_count(0)
    health.get_by_text("Results by available track details", exact=True).click()
    expect(health.get_by_text("Genre, year, audio", exact=True)).to_be_visible()
    health.locator(".model-health-tuning").screenshot(path=str(tmp_path / f"model-selection-{width}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"taste-health-{width}.png"), full_page=True)
    expect(health.get_by_role("heading", name="Recent sound", exact=True)).to_have_count(0)
    expect(health.get_by_role("heading", name="Playlist influence", exact=False)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    health.get_by_role("link", name="Review learning settings", exact=True).click()
    expect(page.get_by_role("tab", name="Recent & learning", exact=True)).to_have_attribute("aria-selected", "true")
    sound = page.locator("#taste-recent-sound")
    sound.scroll_into_view_if_needed()
    expect(sound.locator(".chain-row .vb-name")).to_have_count(2)
    expect(sound.locator(".chain-head")).to_contain_text("Session")
    expect(sound.locator(".chain-head")).to_contain_text("Recent")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    sound.locator("..").screenshot(path=str(tmp_path / f"recent-sound-{width}.png"))
    sound.get_by_role("link", name="Listening styles in Taste profile", exact=True).click()
    influence = page.locator("#taste-playlist-influence")
    influence.scroll_into_view_if_needed()
    expect(influence.locator(".playlist-influence-title")).to_have_text(["Weekend mix"])
    expect(influence.locator(".playlist-influence-share")).to_contain_text("100%")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    influence.locator("..").screenshot(path=str(tmp_path / f"playlist-influence-{width}.png"))
    page.goto(live_app + "/taste#health")
    page.get_by_role("link", name="History and test details", exact=True).click()
    table = page.get_by_role("table", name="Automatic model comparison", exact=True)
    expect(table).to_be_visible()
    expect(table.get_by_role("row").filter(has_text="Track co-occurrence")).to_contain_text("Not enough evidence")
    expect(page.get_by_text("A new track with an extended title", exact=True)).to_be_visible()
    expect(page.locator(".selection-history details")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("link", name="Back to Model health", exact=True).click()
    expect(page.get_by_role("tab", name="Model health", exact=True)).to_have_attribute("aria-selected", "true")
    assert errors == []


def test_model_health_checks_can_retry_without_reloading_the_tab(live_app, page):
    page.route("**/taste/recall", lambda route: route.fulfill(status=503, body="Unavailable"))
    page.goto(live_app + "/taste#health")
    expect(page.get_by_text("No track similarity model has been built yet.", exact=False)).to_be_visible()
    page.locator("#model-health-checks").scroll_into_view_if_needed()
    expect(page.get_by_role("status").filter(has_text="Could not load the model checks.")).to_be_visible()
    page.unroute("**/taste/recall")
    page.get_by_role("button", name="Try checks again", exact=True).click()
    expect(page.get_by_text("Not enough history", exact=True)).to_be_visible()
    expect(page.get_by_text("Not enough playlist data", exact=True)).to_be_visible()
    expect(page.get_by_text("No recommendations tracked yet", exact=True)).to_be_visible()


def test_model_health_remembers_running_selection_after_reload(live_app, store, page):
    run = {"ran_at": 1, "status": "running", "previous": {"method": "svd", "dim": 48},
           "selected": {"method": "svd", "dim": 48}, "grid": [], "progress": "Comparing setup 2 of 4."}
    run_id = store.rec.model.start_selection(1, run)
    paths = []
    page.on("request", lambda request: paths.append(urlsplit(request.url).path))
    page.goto(live_app + "/taste#health")
    expect(page.get_by_role("heading", name="Comparing model setups", exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Comparing model setups", exact=True)).to_be_visible()
    run.update(status="retained", reason="No reliable improvement; current model kept.")
    store.rec.model.save_selection(run_id, run)
    store.set_setting("rec_embed_method", "item2vec")
    store.set_setting("rec_dim", "2")
    store.replace_rec_vectors([("new track|artist", np.array([1, 0], dtype=np.float32).tobytes())])
    expect(page.get_by_role("heading", name="Current model retained", exact=True)).to_be_visible(timeout=10000)
    expect(page.locator(".model-health-facts > div").first.locator("strong")).to_have_text("1")
    expect(page.locator(".model-health-facts strong").nth(1)).to_have_text("Item2Vec")
    expect(page.locator(".model-health-facts strong").nth(2)).to_have_text("2 dimensions")
    expect(page.get_by_text("No later measurements yet", exact=True)).to_be_visible()
    assert "/taste/viz/engine" not in paths


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("state", ["waiting", "insufficient", "retained", "paused"])
def test_automatic_selection_explains_waiting_and_empty_measurements(live_app, store, page, width, state, tmp_path):
    if state != "waiting":
        store.rec.model.start_selection(1, {
            "ran_at": 1, "status": "insufficient" if state == "insufficient" else "retained",
            "previous": {"method": "svd", "dim": 48}, "selected": {"method": "svd", "dim": 48},
            "reason": "More comparable listening evidence needed." if state == "insufficient" else "No reliable improvement; current setup kept.",
            "grid": [], "baseline": {"trials": 29, "windows": 3},
            "comparison": {"eligible": True, "trials": 80, "windows": 3, "previous_score": .4, "score": .4}})
    if state == "insufficient":
        # Reproduce the screenshot: library/history gates pass, but the actual test is short.
        iid = store.get_identities()[0].id
        store.add_history_snapshot(iid, 1 - 15 * 86400, ["earlier|artist"])
        store.add_history_snapshot(iid, 1, ["later|artist"])
        store.replace_rec_vectors([(f"track {i}|artist", np.ones(2, dtype=np.float32).tobytes()) for i in range(100)])
        store.set_setting("rec_dim", "128")
        store.set_setting(autotune_run.RESULT_SETTING, json.dumps({
            "ran_at": -1000, "winner": {"method": "svd", "dim": 128, "recall": .787},
            "previous": {"method": "svd", "dim": 128, "recall": .787},
            "grid": [], "recs": {"compared": 0}}))
    if state == "paused":
        store.set_setting("rec_selection_paused", "1")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#health")
    section = page.locator(".model-health-tuning")
    if state == "waiting":
        expect(section.get_by_role("heading", name="First comparison hasn’t run yet", exact=True)).to_be_visible()
        expect(section).to_contain_text("0 / 14 days")
        expect(section).to_contain_text("0 / 100 tracks")
    elif state == "insufficient":
        expect(section.get_by_role("heading", name="More listening needed", exact=True)).to_be_visible()
        expect(section.locator("#selection-test-tracks")).to_have_attribute("value", "29")
        expect(section.locator("#selection-test-tracks")).to_have_attribute("max", "50")
        expect(section).to_contain_text("3 test periods available · 2 required")
        expect(section.locator("progress")).to_have_count(1)
        expect(section.get_by_text("Playlist patterns", exact=True)).to_have_count(1)
        expect(section).not_to_contain_text("78.7%")
        if width == 1440:
            assert section.bounding_box()["height"] < 500
    else:
        expect(section.get_by_text("No later measurements yet", exact=True)).to_be_visible()
        table = section.get_by_role("table", name="Model prediction comparison", exact=True)
        expect(table.get_by_role("row", name="Current", exact=False)).to_contain_text("40.0%")
        expect(table.locator("tbody tr")).to_have_count(1)
    if state == "paused":
        expect(section.get_by_role("button", name="Resume automatic selection", exact=True)).to_be_visible()
    expect(section.get_by_role("button", name="Compare model setups", exact=True)).to_have_count(0)
    expect(section.locator("details")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    section.screenshot(path=str(tmp_path / f"selection-{state}-{width}.png"))


def test_model_selection_restore_and_resume_controls(live_app, store, page, monkeypatch):
    from yt_playlist.rec.rec_worker import RecWorker
    old = [("old|artist", np.array([1, 0], dtype=np.float32).tobytes())]
    store.replace_rec_vectors(old)
    result = {"ran_at": 1, "status": "applied", "previous": {"method": "svd", "dim": 2},
              "selected": {"method": "svd", "dim": 3}, "reason": "Measured improvement.", "grid": []}
    run_id = store.rec.model.start_selection(1, result)
    store.rec.model.save_selection(run_id, result, rows=[("new|artist", np.ones(3, dtype=np.float32).tobytes())])

    def complete_restore(self):
        requested = store.get_setting("rec_selection_restore")
        if requested:
            store.rec.model.restore_model_selection(int(requested), 2)

    monkeypatch.setattr(RecWorker, "trigger", complete_restore)
    page.goto(live_app + "/taste#health")
    page.get_by_role("button", name="Restore previous model", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(page.get_by_role("heading", name="Automatic selection paused", exact=True)).to_be_visible()
    assert store.get_rec_vectors() == old
    assert store.get_setting("rec_dim") == "2"
    page.get_by_role("button", name="Resume automatic selection", exact=True).click()
    expect(page.get_by_role("heading", name="Previous model restored", exact=True)).to_be_visible()
    assert store.get_setting("rec_selection_paused") == ""


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_feedback_uses_track_table_with_artwork_sorting_and_unban(live_app, store, page, width, tmp_path):
    title = 'Alpha "Live" & the extended recording'
    artist = "Artist's Band"
    key = identity_key(title, artist)
    store.upsert_track("feedback-alpha", title, artist, "First Album", None,
                       thumbnail="/static/nag/intro.png")
    store.upsert_track("feedback-zeta", "Zeta Song", "Another Artist", "Last Album", None,
                       thumbnail="/static/nag/trends.png")
    store.record_dislike(key, until=2_000_000_000, now=2.0)
    store.record_dislike("zeta song|another artist", until=None, now=3.0)
    store.record_dislike("removed song|former artist", until=None, now=1.0)
    store.record_like("liked|artist", now=1.0)
    store.rec.model.mark_graduated_once("liked|artist", "like_grad", now=1.0)
    for kind in ("dismiss", "not_now", "more", "less", "mute"):
        store.record_feedback("for_you", kind + "|artist", kind, now=1.0)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_app + "/taste#feedback")
    table = page.get_by_role("table", name="Banned tracks", exact=False)
    expect(table).to_be_visible()
    rows = table.locator("tbody tr.srow")
    expect(rows).to_have_count(3)
    expect(rows.first).to_contain_text("Zeta Song")
    expect(rows.nth(1).get_by_role("link", name="Artist's Band", exact=True)).to_have_attribute("href", "/artist?name=Artist%27s%20Band")
    assert table.locator("img.pl-thumb").evaluate_all("images => images.every(img => img.complete && img.naturalWidth > 0)")
    expect(table.locator("img.pl-thumb")).to_have_count(2)
    table.get_by_role("button", name="Song", exact=True).click()
    expect(rows.first).to_contain_text(title)
    table.get_by_role("button", name="Song", exact=True).press("Enter")
    expect(rows.first).to_contain_text("Zeta Song")
    expect(page.get_by_text("Liked tracks", exact=True)).to_be_visible()
    expect(page.get_by_text("Snoozed suggestions", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"taste-feedback-{width}.png"), full_page=True)
    page.get_by_role("button", name=f"Unban {title}", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(table.locator("tbody tr.srow")).to_have_count(2)
    assert key not in store.disliked_identity_keys()
    expect(page.get_by_role("tab", name="Feedback", exact=True)).to_have_attribute("aria-selected", "true")
    page.get_by_role("button", name="Clear feedback and recommendation history", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(table.get_by_text("No banned tracks.", exact=True)).to_be_visible()
    assert not store.disliked_identity_keys()
    assert errors == []


@pytest.mark.parametrize("width", [1920, 1440, 1024, 390, 320])
def test_taste_topics_show_one_group_at_a_time(store, page, monkeypatch, request, width, tmp_path):
    from yt_playlist.rec.rec_worker import RecWorker

    # Keep the synthetic layout fixture stable; startup group repair is tested separately.
    monkeypatch.setattr(RecWorker, "trigger_mode_refresh", lambda self, **kw: None)
    live_app = request.getfixturevalue("live_app")
    iid = store.get_identities()[0].id
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
            track = store.upsert_track(video_id, title, artist, None, None, thumbnail=thumbnail)
            store.set_track_genre(track, genre)
            store.set_track_year(track, year)
            key = identity_key(title, artist)
            rep_keys.append(key)
            content_vectors.append((key, vectors[i].tobytes()))
            recent_plays.append((key, video_id, 1.0))
        store.add_history_snapshot(iid, 1.0, rep_keys)
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
    store.replace_rec_content_vectors(content_vectors)
    store.import_play_events(iid, recent_plays[:4])
    store.set_weight("lane:deep_cut", 1.6, now=1.0)
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

    page.get_by_role("tab", name="Generated playlists", exact=True).click()
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
    expect(recommendations.locator(".taste-feature-section")).to_have_count(8)
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
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
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
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#recommendations/features")
    features = page.locator("#taste-feature-settings")
    navigation = features.get_by_role("tablist", name="Features", exact=True)
    expect(navigation.get_by_role("tab")).to_have_count(8)
    expect(features.locator("details")).to_have_count(0)
    expect(features.locator("#taste-feature-radio")).to_have_count(0)
    # Track length is now a Home control too; keep it available alongside refresh/variety/comfort.
    expect(features.get_by_role("slider")).to_have_count(15)
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
    expect(features.get_by_role("slider")).to_have_count(4)
    page.keyboard.press("End")
    expect(navigation.get_by_role("tab", name="Generated playlist cleanup", exact=True)).to_be_focused()
    expect(features.get_by_role("slider")).to_have_count(1)
    page.keyboard.press("Home")
    expect(home_tab).to_be_focused()
    expect(slider).to_have_value("4")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.reload()
    expect(slider).to_have_value("4")
    with page.expect_response(lambda response: response.url.endswith("/taste/reset-param")):
        home.get_by_role("button", name="Reset Home visits before suggestions refresh to default", exact=True).click()
    expect(slider).to_have_value("3")
    assert rec_params.get_param(store, "erosion_view_cap") == 3


@pytest.mark.parametrize("query", ["", "?radio=true", "?radio=1"])
def test_radio_settings_are_absent_even_with_the_old_opt_in(live_app, page, query):
    page.goto(live_app + "/taste" + query + "#recommendations/features")
    expect(page.locator("#taste-feature-home")).to_be_visible()
    expect(page.get_by_role("tab", name="Radio", exact=True)).to_have_count(0)
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
    iid = store.get_identities()[0].id
    track = store.upsert_track("genre-test", "Genre test", "Test artist", None, None)
    store.set_track_genre(track, "Techno")
    store.add_history_snapshot(iid, 1.0, ["genre test|test artist"])
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

    page.get_by_role("tab", name="Generated playlists", exact=True).click()
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
    assert store.get_weights(now=1.0)["genre:techno"] == 0.8
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
    store.set_weight("genre:rock", 0, lo=0, now=-100)
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
    assert store.weight_records()["genre:rock"]["weight"] == 0


def test_taste_loads_diagnostics_only_when_opened(live_app, page):
    paths = {"/taste/viz/engine", "/taste/viz/recent-sound", "/taste/viz/playlist-influence",
             "/taste/recall", "/taste/preview", "/taste/comparison"}
    requests = []
    page.on("request", lambda request: requests.append(urlsplit(request.url).path)
            if urlsplit(request.url).path in paths else None)
    page.goto(live_app + "/taste")
    expect(page.locator("#taste-overview-panel")).to_be_visible()
    assert requests == []
    page.get_by_role("tab", name="Model health", exact=True).click()
    expect(page.locator("#taste-health-panel").get_by_text("Learning method", exact=True)).to_be_visible()
    page.locator("#model-health-checks").scroll_into_view_if_needed()
    expect(page.locator("#model-status")).to_contain_text("Future listening")
    assert requests == ["/taste/recall"]
    page.get_by_role("tab", name="Overview", exact=True).click()
    page.get_by_role("tab", name="Model health", exact=True).click()
    expect(page.locator("#taste-health-panel").get_by_text("Learning method", exact=True)).to_be_visible()
    assert requests == ["/taste/recall"]

    page.get_by_role("tab", name="Taste profile", exact=True).click()
    influence = page.locator("#taste-playlist-influence")
    influence.scroll_into_view_if_needed()
    expect(influence).to_contain_text("Build the model and play some playlists")
    assert requests == ["/taste/recall", "/taste/viz/playlist-influence"]
    page.get_by_role("tab", name="Recent & learning", exact=True).click()
    sound = page.locator("#taste-recent-sound")
    sound.scroll_into_view_if_needed()
    expect(sound).to_contain_text("Not enough recent listening to compare sounds yet")
    assert requests == ["/taste/recall", "/taste/viz/playlist-influence", "/taste/viz/recent-sound"]
    page.get_by_role("tab", name="Taste profile", exact=True).click()
    influence.scroll_into_view_if_needed()
    expect(influence).to_contain_text("Build the model and play some playlists")
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
