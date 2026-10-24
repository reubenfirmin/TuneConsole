"""Model health diagnostics, automatic selection and recovery controls."""
import json
from urllib.parse import urlsplit

import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.rec import autotune_run, eval_recs, scorecard

@pytest.mark.parametrize("width", [1920, 1440, 1024, 390, 320])
def test_model_health_groups_results_and_explains_tuning(live_app, store, page, monkeypatch, width, tmp_path):
    iid = store.identities.get_identities()[0].id
    tracks, vectors = [], []
    for i in range(24):
        tid = store.tracks.upsert_track(f"health-{i}", f"Track {i}", f"Artist {i % 4}", "Album", None)
        tracks.append(tid)
        store.tracks.set_track_genre(tid, "Jazz" if i % 2 else "Techno")
        vectors.append((f"track {i}|artist {i % 4}", np.eye(4, dtype=np.float32)[i % 4].tobytes()))
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "health-mix", "Weekend mix", 24, "health", 0), tracks)
    store.rec_model.replace_rec_vectors(vectors)
    store.settings.set_setting("rec_dim", "4")
    store.settings.set_setting("rec_embed_method", "svd")
    store.history.record_history_plays(iid, 0, [key for key, _ in vectors], source_timezone="UTC")
    monkeypatch.setattr(eval_recs, "recall_at_k", lambda *args, **kwargs:
                        {"recall_at_k": .68, "k": 20, "trials": 42, "lift": 2.4})
    monkeypatch.setattr(eval_recs, "rolling_temporal_recall", lambda *args, **kwargs:
                        {"recall": .34, "k": 20, "trials": 120, "viable_windows": 2,
                         "requested_windows": 3, "generated_excluded": 8})
    monkeypatch.setattr(eval_recs, "projection_recall", lambda *args, **kwargs:
                        {"recall": .52, "k": 20, "trials": 80, "breakdown": {"by_coverage": {
                            "genre": {"recall": .40, "trials": 40},
                            "genre+year+audio": {"recall": .64, "trials": 40}}}})
    monkeypatch.setattr(scorecard, "recommendation_scorecard", lambda *args, **kwargs:
                        {"impressions": 80, "played": 16, "acceptance_rate": .20,
                         "organic": 10, "generated": 8, "powered": False, "min_impressions": 100,
                         "open": 8, "start": 10, "save": 4, "promotion": 1, "conversion": 2,
                         "cold_impressions": 8, "unique_artists": 12, "unique_genres": 3,
                         "by_ownership": {"cold": {"played": 2, "impressions": 8, "powered": False}},
                         "by_metadata": {"genre": {"missing": {"played": 1, "impressions": 5,
                                                                  "powered": False}}}})
    monkeypatch.setattr(store.rec_signals, "graduation_log_counts", lambda: {"play": 38, "like": 8, "slider": 3})
    store.settings.set_setting(autotune_run.RESULT_SETTING, json.dumps({
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
    run_id = store.model_selection.start_selection(run["ran_at"], run)
    store.settings.set_setting("rec_selection_active_run", str(run_id))
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
    run_id = store.model_selection.start_selection(1, run)
    paths = []
    page.on("request", lambda request: paths.append(urlsplit(request.url).path))
    page.goto(live_app + "/taste#health")
    expect(page.get_by_role("heading", name="Comparing model setups", exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Comparing model setups", exact=True)).to_be_visible()
    run.update(status="retained", reason="No reliable improvement; current model kept.")
    store.model_selection.save_selection(run_id, run)
    store.settings.set_setting("rec_embed_method", "item2vec")
    store.settings.set_setting("rec_dim", "2")
    store.rec_model.replace_rec_vectors([("new track|artist", np.array([1, 0], dtype=np.float32).tobytes())])
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
        store.model_selection.start_selection(1, {
            "ran_at": 1, "status": "insufficient" if state == "insufficient" else "retained",
            "previous": {"method": "svd", "dim": 48}, "selected": {"method": "svd", "dim": 48},
            "reason": "More comparable listening evidence needed." if state == "insufficient" else "No reliable improvement; current setup kept.",
            "grid": [], "baseline": {"trials": 29, "windows": 3},
            "comparison": {"eligible": True, "trials": 80, "windows": 3, "previous_score": .4, "score": .4}})
    if state == "insufficient":
        # Reproduce the screenshot: library/history gates pass, but the actual test is short.
        iid = store.identities.get_identities()[0].id
        store.history.record_history_plays(iid, 1 - 15 * 86400, ["earlier|artist"], source_timezone="UTC")
        store.history.record_history_plays(iid, 1, ["later|artist"], source_timezone="UTC")
        store.rec_model.replace_rec_vectors([(f"track {i}|artist", np.ones(2, dtype=np.float32).tobytes()) for i in range(100)])
        store.settings.set_setting("rec_dim", "128")
        store.settings.set_setting(autotune_run.RESULT_SETTING, json.dumps({
            "ran_at": -1000, "winner": {"method": "svd", "dim": 128, "recall": .787},
            "previous": {"method": "svd", "dim": 128, "recall": .787},
            "grid": [], "recs": {"compared": 0}}))
    if state == "paused":
        store.settings.set_setting("rec_selection_paused", "1")
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
    store.rec_model.replace_rec_vectors(old)
    result = {"ran_at": 1, "status": "applied", "previous": {"method": "svd", "dim": 2},
              "selected": {"method": "svd", "dim": 3}, "reason": "Measured improvement.", "grid": []}
    run_id = store.model_selection.start_selection(1, result)
    store.model_selection.save_selection(run_id, result, rows=[("new|artist", np.ones(3, dtype=np.float32).tobytes())])

    def complete_restore(self):
        requested = store.settings.get_setting("rec_selection_restore")
        if requested:
            store.model_selection.restore_model_selection(int(requested), 2)

    monkeypatch.setattr(RecWorker, "trigger", complete_restore)
    page.goto(live_app + "/taste#health")
    page.get_by_role("button", name="Restore previous model", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(page.get_by_role("heading", name="Automatic selection paused", exact=True)).to_be_visible()
    assert store.rec_model.get_rec_vectors() == old
    assert store.settings.get_setting("rec_dim") == "2"
    page.get_by_role("button", name="Resume automatic selection", exact=True).click()
    expect(page.get_by_role("heading", name="Previous model restored", exact=True)).to_be_visible()
    assert store.settings.get_setting("rec_selection_paused") == ""


