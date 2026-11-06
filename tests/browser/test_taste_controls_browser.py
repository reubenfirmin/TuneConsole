"""Taste topics, keyboard navigation, saved controls and deferred diagnostics."""
import re
from urllib.parse import urlsplit

import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.rec import rec_params
from yt_playlist.util.matching import identity_key













def test_taste_controls_survive_topic_switches_and_reload(live_app, store, page):
    iid = store.accounts.get_accounts()[0].id
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
    page.set_viewport_size({"width": 1280, "height": 900})
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
