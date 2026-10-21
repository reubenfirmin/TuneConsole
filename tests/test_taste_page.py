import json
import numpy as np
import pytest
from html.parser import HTMLParser
from fastapi.testclient import TestClient

from yt_playlist.rec import embed, rec_params, taste_viz
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1")


def test_retired_source_weight_cannot_be_saved_or_learned(store):
    client = _client(store)
    response = client.post("/taste/weight", data={"axis": "lane:explore", "weight": "1.3"})
    assert response.status_code == 400
    assert store.settings.get_setting("taste_settings_checkpoint_v1") is None
    for kind in ("more", "less", "dismiss", "not_now"):
        response = client.post("/recs/feedback", data={"item": "old catalog track|artist",
                                                     "kind": kind, "lane": "explore"})
        assert response.status_code == 200
    assert "lane:explore" not in store.rec_model.weight_records()


def test_taste_page_renders_status_and_controls(store):
    c = _client(store)
    html = c.get("/taste").text
    assert "Taste Model" in html
    assert 'name="weight"' not in html
    assert 'name="weight"' in c.get("/taste/panels/generated").text
    assert "/taste/autotune" in c.get("/taste/panels/health").text


def test_taste_shell_and_overview_do_not_compute_hidden_tabs(store, monkeypatch):
    c = _client(store)

    def unexpected(*args, **kwargs):
        raise AssertionError("Hidden tab data was loaded")

    monkeypatch.setattr(store.rec_query, "rec_baskets", unexpected)
    monkeypatch.setattr(taste_viz, "model_transparency", unexpected)
    monkeypatch.setattr(taste_viz, "generated_playlists", unexpected)
    monkeypatch.setattr(embed, "load_vectors", unexpected)
    assert c.get("/taste").status_code == 200
    response = c.get("/taste/panels/overview")
    assert response.status_code == 200
    assert "Your long-term taste" in response.text
    assert "What’s changed recently?" in response.text
    assert 'type="range"' not in response.text
    experiment = c.get("/taste/panels/experiment")
    assert experiment.status_code == 200
    assert "Your starting point" in experiment.text
    assert "Preview &amp; compare" in experiment.text
    assert "settings across Taste Model" in experiment.text
    assert "/taste/checkpoint/restore" in experiment.text
    assert "/taste/reset-all" in experiment.text
    assert 'aria-label="Experiment tools"' in experiment.text
    for topic in ("starting", "preview"):
        assert f'id="taste-experiment-{topic}-tab"' in experiment.text
        assert f'id="taste-experiment-{topic}-panel"' in experiment.text
    assert c.get("/taste/panels/unknown").status_code == 404


def test_genre_coverage_counts_tracks_instead_of_weighted_plays(store):
    c = _client(store)
    iid = store.identities.get_identities()[0].id
    tagged = store.tracks.upsert_track("tagged", "Tagged", "Artist", None, None)
    store.tracks.set_track_genre(tagged, "Jazz")
    store.tracks.upsert_track("untagged", "Untagged", "Artist", None, None)
    for day in range(4):
        store.history.add_history_snapshot(iid, day * 86400.0, ["tagged|artist"])

    html = c.get("/taste/model-status").text

    assert "Genre coverage" in html
    assert "1 of 2 library tracks have a genre tag" in html
    assert "50%" in html


def test_model_selection_is_automatic_and_old_manual_route_cannot_start_a_sweep(store):
    c = _client(store)
    r = c.post("/taste/autotune")
    assert r.status_code == 409
    assert "automatically" in r.text
    assert "HX-Refresh" not in r.headers
    # Status endpoint is reachable and renders the fragment.
    s = c.get("/taste/autotune-status")
    assert s.status_code == 200


def test_taste_page_shows_transient_and_graduation_controls(store):
    html = _client(store).get("/taste/panels/learning").text
    assert 'class="taste-settings-section taste-settings-layout"' in html
    for label in ("Recent influence", "Listening memory", "Current session", "Saved preferences", "Learning rates"):
        assert f'aria-label="{label}"' in html
    assert '<details' not in html
    # Each setting has one home, including the formerly nested advanced controls.
    for param in rec_params.PARAMS:
        expected = int(param.group in ("transient", "graduation")
                       and param.name not in rec_params.SEMANTIC_PARAMS)
        assert html.count(f'id="param-{param.name}"') == expected, param.name
    assert 'id="param-graduation_enabled"' not in html
    assert "Learning paused" not in html
    assert "Learning enabled" not in html
    assert 'id="param-play_transient_w"' in _client(store).get("/taste/panels/recommendations").text


def test_preference_binding_knob_is_on_the_page_and_round_trips(store):
    """The axis_weight_cap knob decides whether a stated genre preference tilts the ranking or
    replaces it, so it remains reachable and reversible in diagnostic controls."""
    from yt_playlist.rec import rec_params
    from yt_playlist.rec.scoring import AXIS_WEIGHT_UNBOUNDED
    c = _client(store)

    html = c.get("/taste/panels/recommendations").text
    assert 'aria-label="Ranking &amp; feedback"' in html
    assert "Fine-tune your preferences" not in html
    assert "Preference strength" in html
    assert 'id="param-axis_weight_cap"' in html
    assert rec_params.get_param(store, "axis_weight_cap") == 1.4

    assert c.post("/taste/param", data={"name": "axis_weight_cap", "value": "1.4"}).status_code == 200
    assert rec_params.get_param(store, "axis_weight_cap") == 1.4

    assert c.post("/taste/reset-param", data={"name": "axis_weight_cap"}).status_code == 200
    assert rec_params.get_param(store, "axis_weight_cap") == 1.4


def test_taste_page_separates_semantic_controls_from_topic_settings(store):
    html = _client(store).get("/taste/panels/recommendations").text
    assert "Main controls" in html
    assert html.count('role="tablist"') == 2
    assert 'aria-label="Recommendation settings"' in html
    assert 'aria-label="Features"' in html
    for topic in ("intro", "main", "genres", "features"):
        assert f'id="taste-recommendation-{topic}-tab"' in html
        assert f'id="taste-recommendation-{topic}-panel"' in html
    assert "Home and Discover" in html and 'href="#experiment"' in html
    assert 'id="taste-checkpoint"' not in html
    assert 'id="taste-preview"' not in html
    assert "/taste/reset-all" not in html
    for name in rec_params.SEMANTIC_PARAMS:
        assert html.count(f'id="param-{name}"') == 1


def test_maximum_track_length_setting_controls_generated_playlists(store):
    from yt_playlist.library.executor import create_generated_playlist

    client = _client(store)
    html = client.get("/taste/panels/recommendations").text
    panel = html.split('id="taste-feature-home"', 1)[1].split('</section>', 1)[0]
    assert 'id="param-max_track_minutes"' in panel
    assert "Maximum track length (minutes)" in panel
    assert 'Default 20' in panel
    assert client.post("/taste/param", data={"name": "max_track_minutes", "value": "120"}).status_code == 200

    remote = FakeClient(song_durations={"concert": 7200})
    result = create_generated_playlist(store, "Gen", [{"video_id": "concert"}], remote, 1)
    assert result["added"] == 1
    assert client.post("/taste/reset-param", data={"name": "max_track_minutes"}).status_code == 200
    assert rec_params.get_param(store, "max_track_minutes") == 20
    with pytest.raises(ValueError, match="maximum track length"):
        create_generated_playlist(store, "Gen", [{"video_id": "concert"}], remote, 2)
    assert len(remote.created) == 1


def test_autotune_done_status_refreshes_model_status(store):
    c = _client(store)
    # The completed (idle) status poll fires the event the Model status card listens for.
    s = c.get("/taste/autotune-status")
    assert s.headers.get("HX-Trigger") == "autotune-done"
    # The Model status fragment renders and re-arms its own auto-refresh listener.
    m = c.get("/taste/model-status")
    assert m.status_code == 200
    assert 'id="model-status"' in m.text
    assert "Tracks modeled" in m.text
    # The page wires the card to listen for autotune-done.
    page = c.get("/taste/panels/health").text
    assert "autotune-done from:body" in page


def test_taste_page_empty_autotune_state(store):
    html = _client(store).get("/taste/panels/health").text
    assert "First comparison hasn’t run yet" in html
    assert 'value="0" max="14"' in html
    assert "Compare model setups</button>" not in html


def test_taste_page_renders_autotune_result_panel(store):
    from yt_playlist.rec import autotune_run
    iid = store.identities.upsert_identity("main", "cred", None, True)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(40)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(40)]
    for j in range(6):
        store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, f"PA{j}", "PA", 8, f"ha{j}", 0.0), A[j*5:j*5+8])
        store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, f"PB{j}", "PB", 8, f"hb{j}", 0.0), B[j*5:j*5+8])
    autotune_run.run_and_record(store, now=1000.0)
    client = _client(store)
    main = client.get("/taste/autotune-status").text
    assert "Earlier manual Auto-tune result" not in main
    assert '<details' not in main
    assert 'href="/taste/model-selection/history"' in main
    html = client.get("/taste/model-selection/history").text
    assert "Playlist patterns (SVD)" in html
    assert "Selected model" in html and "Previous model" in html
    assert "percentage points" in html
    # No history was recorded, so the sweep judged itself in-sample: the panel must say so.
    assert "This test used your existing playlists" in html


def _set_autotune_result(store, **overrides):
    import json
    from yt_playlist.rec import autotune_run
    payload = {
        "ran_at": 1000.0,
        "winner": {"method": "svd", "dim": 64, "recall": 0.5, "metric": "temporal_recall"},
        "previous": {"method": "svd", "dim": 48, "recall": 0.3, "metric": "temporal_recall"},
        "grid": [{"method": "svd", "dim": 64, "recall": 0.5, "metric": "temporal_recall"},
                 {"method": "svd", "dim": 48, "recall": 0.3, "metric": "temporal_recall"}],
        "metric": "temporal_recall",
        "in_sample": False,
        "recs": {"dropped": [], "added": [], "compared": 0},
    }
    payload.update(overrides)
    store.settings.set_setting(autotune_run.RESULT_SETTING, json.dumps(payload))


def test_taste_page_autotune_temporal_result_has_no_in_sample_warning(store):
    _set_autotune_result(store)
    html = _client(store).get("/taste/model-selection/history").text
    assert "predict later listening from earlier data" in html
    assert "This test used your existing playlists" not in html
    assert "The model comparison failed" not in html


def test_taste_page_autotune_sweep_failed_renders_line(store):
    _set_autotune_result(store, metric="recall_at_k", in_sample=True, sweep_failed=True,
                          winner={"method": "svd", "dim": 48, "recall": 0.2, "metric": "recall_at_k"},
                          previous={"method": "svd", "dim": 48, "recall": 0.2, "metric": "recall_at_k"})
    html = _client(store).get("/taste/model-selection/history").text
    assert "The model comparison failed. The previous settings were kept." in html
    assert "Selected <strong>" not in html
    assert "top suggestions captured" not in html
    assert "Applied &amp; saved" not in html


def test_autotune_report_distinguishes_failed_scores_and_unscored_baseline(store):
    _set_autotune_result(store,
                        previous={"method": "svd", "dim": 48, "recall": 0, "failed": True},
                        grid=[{"method": "svd", "dim": 64, "recall": 0.5},
                              {"method": "svd", "dim": 48, "recall": 0, "failed": True},
                              {"method": "item2vec", "dim": 64, "recall": 0}])
    html = _client(store).get("/taste/model-selection/history").text
    assert html.count("Not scored") == 2
    assert "Test unavailable" in html
    assert "0.0%" in html  # A genuine zero remains a score; an unavailable test does not.
    assert "percentage points" not in html
    assert 'datetime="1970-01-01T00:16:40+00:00"' in html


def test_autotune_report_explains_historical_test_and_recommendation_changes(store):
    _set_autotune_result(store, metric="causal_temporal_recall",
                        recs={"compared": 15,
                              "added": [{"title": 'New <track>', "artist": "Artist & friend"}],
                              "dropped": [{"title": "Old track", "artist": "Another artist"}]})
    html = _client(store).get("/taste/model-selection/history").text
    assert "only data available at that time" in html
    assert "+20.0" in html and "percentage points" in html
    assert "1 of the 15 top suggestions" in html
    assert "New &lt;track&gt;" in html and "Artist &amp; friend" in html


def test_taste_page_uses_sliders_not_number_fields(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste/panels/recommendations").text
    assert 'type="range"' in html                 # knobs are sliders now
    assert 'type="number"' not in html            # ...not typed number fields
    assert "/taste/param" in html                 # scalar knobs post here
    assert "Source weights" in c.get("/taste/panels/generated").text
    assert "Settings by feature" in html


def test_taste_page_renders_genre_slider_for_tagged_library(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Techno")
    store.history.add_history_snapshot(iid, 1.0, ["song|band"])    # play it so it counts toward the mix
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste/panels/recommendations").text
    assert 'genre:techno' in html                 # a per-genre-family weight control is rendered


def test_taste_controls_round_trip(store):
    c = _client(store)
    assert c.post("/taste/weight", data={"axis": "lane:deep_cut", "weight": "0.5"}).status_code == 200
    assert store.rec_model.get_weights(now=1.0)["lane:deep_cut"] == 0.5
    assert c.post("/taste/reset-weights").status_code == 200
    assert store.rec_model.get_weights() == {}
    store.rec_model.record_feedback("for_you", "a|b", "dismiss", now=1.0)
    assert c.post("/taste/clear-feedback").status_code == 200
    assert store.rec_model.suppressed_keys("for_you", now=2.0) == set()


def test_taste_recall_fragment(store):
    c = _client(store)
    r = c.get("/taste/recall")
    assert r.status_code == 200   # no vectors -> "build first", still 200
    assert "1-day test period" in r.text


def test_taste_param_saves_and_marks_sample_stale(store):
    c = _client(store)
    r = c.post("/taste/param", data={"name": "comfort_recency_full_days", "value": "60"})
    assert r.status_code == 200
    assert r.headers.get("hx-refresh") != "true"          # no full reload (the old "flashing" cause)
    assert r.headers.get("hx-trigger") == "taste-stale"   # marks the live sample stale instead
    assert rec_params.get_param(store, "comfort_recency_full_days") == 60


def test_taste_param_is_clamped(store):
    c = _client(store)
    c.post("/taste/param", data={"name": "palette_absence_penalty", "value": "9"})
    assert rec_params.get_param(store, "palette_absence_penalty") == 2.0   # max


def test_taste_genre_weight_uses_genre_band(store):
    c = _client(store)
    # a genre axis may be muted to 0 (band [0,2]); the lane band [0.2,3.0] must not floor it
    assert c.post("/taste/weight", data={"axis": "genre:rock", "weight": "0"}).status_code == 200
    assert store.rec_model.get_weights(now=1.0)["genre:rock"] == 0.0


def test_taste_reset_param_restores_default(store):
    c = _client(store)
    rec_params.set_param(store, "comfort_min_plays", 3)
    assert c.post("/taste/reset-param", data={"name": "comfort_min_plays"}).status_code == 200
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_taste_reset_all_clears_weights_and_params(store):
    c = _client(store)
    store.rec_model.set_weight("lane:deep_cut", 0.5)
    rec_params.set_param(store, "comfort_min_plays", 3)
    assert c.post("/taste/reset-all").status_code == 200
    assert store.rec_model.get_weights() == {}
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_taste_preview_renders_without_recs(store):
    c = _client(store)
    assert c.get("/taste/preview").status_code == 200     # no model yet -> still 200


def test_taste_preview_explains_scope_sources_and_two_different_samples(store):
    html = _client(store).get("/taste/panels/experiment").text
    preview = html.split('id="taste-preview"', 1)[1]
    for text in ("Preview &amp; compare", "Home’s familiar-music mix", "Taste matches",
                 "Deep cuts", "Playlist neighbors", "recent-listening influence and memory",
                 "Source weights in Generated playlists", "Up to 8 songs", "top 24 picks"):
        assert text in preview
    for destination in ("#recommendations/main", "#recommendations/genres", "#recommendations/features",
                        "#learning", "#generated"):
        assert f'href="{destination}"' in preview


def test_taste_preview_explains_production_score_trace_in_plain_language(store, monkeypatch):
    from yt_playlist.rec import surfaces
    trace = {"durable": 0.4, "transient_delta": 0.1, "session_delta": -0.02,
             "audio_delta": 0.03, "combined": 0.51, "rank_base": 0.8,
             "axes": {"genre": 1.2, "era": 1.0},
             "axis_labels": {"genre": "electronic", "era": "2010s"},
             "axis_product": 1.2, "axis_applied": 1.2, "axis_cap": 1.4,
             "final": 0.96, "contexts": [{"name": "Night drive", "contribution": 0.3}]}
    item = surfaces.ForYouItem("Track", "Artist", "", "v", None, 0, "reason",
                                key="track|artist", lane="neighbourhood", trace=trace)
    monkeypatch.setattr(surfaces, "taste_sample", lambda *a, **kw: [item])

    html = _client(store).get("/taste/preview").text

    assert "Why this pick" in html
    assert "Night drive" in html and "recent listening favors it" in html
    assert "This session makes it a weaker match" in html
    assert "genre preference favors it (electronic)" in html
    assert "<details" not in html


def test_taste_comparison_keeps_fixed_baseline_and_shows_delta(store, monkeypatch):
    from yt_playlist.rec import surfaces
    calls = []

    def comparison(_store, _now, keys=None, limit=24):
        calls.append(keys)
        score = 0.8 if keys is None else 0.9
        durable = 0.4 if keys is None else 0.5
        return [{"key": "track|artist", "title": "Track", "artist": "Artist",
                 "rank": 1, "in_cohort": True, "score": score, "trace": {"durable": durable,
                 "transient_delta": 0.1, "session_delta": 0.0, "audio_delta": 0.0,
                 "rank_base": 0.7, "axes": {"genre": 1.1}, "axis_applied": 1.1},
                 "lane": "neighbourhood", "lane_weight": 1.0,
                 "eligible": True, "eligibility_reason": None}]

    monkeypatch.setattr(surfaces, "taste_comparison", comparison)
    c = _client(store)
    assert c.post("/taste/comparison/baseline").status_code == 200
    html = c.get("/taste/comparison").text
    assert calls[-1] == ["track|artist"]
    assert "#1 to #1" in html and "saved taste match increased" in html
    assert "captured picks stay in the comparison" in html
    assert "<details" not in html
    assert "Taste matches" in html


def test_recommendation_controls_are_all_available_and_rendered_once(store):
    class RecommendationPanel(HTMLParser):
        def __init__(self):
            super().__init__()
            self.details = []
            self.features = []
            self.ids = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "details":
                self.details.append(attrs)
            if tag == "section" and attrs.get("class") == "taste-feature-section":
                self.features.append(attrs)
            if tag == "input":
                self.ids.append(attrs.get("id"))

    client = _client(store)
    for query in ["", "?radio=false", "?radio=0", "?radio=true", "?radio=1"]:
        panel = RecommendationPanel()
        html = client.get("/taste/panels/recommendations" + query).text
        panel.feed(html)
        assert panel.details == []
        assert len(panel.features) == 8
        assert all(f.get("role") == "tabpanel" and f.get("aria-labelledby") == f'{f["id"]}-tab'
                   for f in panel.features)
        for p in rec_params.PARAMS:
            if p.group == "discovery" or p.name in rec_params.SEMANTIC_PARAMS:
                expected = 0 if p.name.startswith("radio_") else 1
                assert panel.ids.count(f"param-{p.name}") == expected, p.name
        assert "radio" not in html.lower()
        assert 'hx-get="/taste/panels/recommendations"' in client.get("/taste" + query).text


def test_first_change_saves_recovery_point_and_later_changes_keep_it(store):
    client = _client(store)
    store.rec_model.set_weight("genre:rock", 0, lo=0, now=-1000)
    store.rec_model.set_weight("era:1990s", 1.7, now=-500)
    original_weights = store.rec_model.weight_records()
    rec_params.set_param(store, "breadth_bias", -0.25)
    rec_params.set_param(store, "source_w_play", 0.12)
    store.settings.set_setting("unrelated_setting", "keep me")
    assert "automatically saves" in client.get("/taste/checkpoint").text

    client.post("/taste/param", data={"name": "breadth_bias", "value": "0.8"})
    client.post("/taste/weight", data={"axis": "genre:rock", "weight": "1.5"})
    client.post("/taste/weight", data={"axis": "genre:jazz", "weight": "0"})
    client.post("/taste/param", data={"name": "comfort_min_plays", "value": "12"})
    client.post("/taste/param", data={"name": "source_w_play", "value": "0.2"})
    store.rec_model.record_feedback("for_you", "song|artist", "dismiss", now=1)
    store.rec_model.replace_rec_vectors([("song|artist", np.array([1., 0.], dtype=np.float32).tobytes())])

    assert client.post("/taste/checkpoint/restore").headers["HX-Refresh"] == "true"
    assert rec_params.get_param(store, "breadth_bias") == -0.25
    assert rec_params.get_param(store, "source_w_play") == 0.12
    assert store.settings.get_setting("rec_param:comfort_min_plays") is None
    assert store.rec_model.weight_records() == original_weights  # includes zero and original fade timestamps
    assert store.settings.get_setting("unrelated_setting") == "keep me"
    assert store.rec_model.suppressed_keys("for_you", now=1) == {"song|artist"}
    assert store.rec_model.rec_vectors_count() == 1
    # Reopening the page/app does not discard the checkpoint.
    assert "Starting point saved" in _client(store).get("/taste/checkpoint").text


def test_checkpoint_can_be_replaced_and_survives_reset_all(store):
    client = _client(store)
    client.post("/taste/param", data={"name": "breadth_bias", "value": "0.5"})
    assert client.post("/taste/checkpoint").status_code == 200
    client.post("/taste/reset-all")
    assert rec_params.get_param(store, "breadth_bias") == 0
    client.post("/taste/checkpoint/restore")
    assert rec_params.get_param(store, "breadth_bias") == 0.5


def test_reset_all_itself_saves_a_recovery_point(store):
    client = _client(store)
    store.rec_model.set_weight("artist:someone", 1.6, now=None)
    rec_params.set_param(store, "comfort_min_plays", 9)
    client.post("/taste/reset-all")
    client.post("/taste/checkpoint/restore")
    assert rec_params.get_param(store, "comfort_min_plays") == 9
    assert store.rec_model.weight_records()["artist:someone"]["weight"] == 1.6


def test_taste_comparison_labels_entries_exits_and_exclusions(store, monkeypatch):
    from yt_playlist.rec import surfaces

    trace = {"durable": 0.4, "transient_delta": 0.0, "session_delta": 0.0,
             "audio_delta": 0.0, "rank_base": 0.7, "axes": {}, "axis_applied": 1.0}

    def comparison(_store, _now, keys=None, limit=24):
        if keys is None:
            return [{"key": "old|artist", "title": "Old", "artist": "Artist", "rank": 1,
                     "in_cohort": True, "score": 0.8, "trace": trace, "lane": "deep_cut",
                     "lane_weight": 1.0, "eligible": True, "eligibility_reason": None}]
        return [
            {"key": "new|artist", "title": "New", "artist": "Artist", "rank": 1,
             "in_cohort": True, "score": 0.9, "trace": trace, "lane": "neighbourhood",
             "lane_weight": 1.0, "eligible": True, "eligibility_reason": None},
            {"key": "old|artist", "title": "Old", "artist": "Artist", "rank": None,
             "in_cohort": False, "score": 0.8, "trace": trace, "lane": "",
             "lane_weight": None, "eligible": False, "eligibility_reason": "dismissed or snoozed"},
        ]

    monkeypatch.setattr(surfaces, "taste_comparison", comparison)
    client = _client(store)
    client.post("/taste/comparison/baseline")
    html = client.get("/taste/comparison").text
    assert "Added to the top 24 at #1" in html
    assert "excluded: dismissed or snoozed" in html
    assert "NEW" in html and "OUT" in html


def test_taste_page_lists_and_clears_bans(store):
    store.tracks.upsert_track("banned-video", "Song", "Band", "An Album", None,
                       thumbnail="/static/banned-cover.jpg")
    store.rec_model.record_dislike("song|band", until=99999.0, now=1.0)
    c = _client(store)
    html = c.get("/taste/panels/feedback").text
    assert '<table class="taste-feedback-table"' in html
    assert "Song" in html and "Band" in html and "An Album" in html
    assert 'src="/static/banned-cover.jpg"' in html
    assert "https://music.youtube.com/watch?v=banned-video" in html
    assert c.post("/taste/unban", data={"key": "song|band"}).status_code == 200
    assert store.rec_model.disliked_identity_keys() == set()
    assert "No banned tracks." in c.get("/taste/panels/feedback").text


def test_feedback_tracks_resolve_discovery_and_keep_missing_metadata_manageable(store):
    store.discovery.upsert_discovered_track("new song|new artist", "discovered-video", "New Song", "New Artist",
                                  "New Album", "/static/discovered-cover.jpg", None, None, "", now=1.0)
    store.rec_model.record_dislike("new song|new artist", until=None, now=2.0)
    store.rec_model.record_dislike("removed song|former artist", until=0.0, now=3.0)
    response = _client(store).get("/taste/panels/feedback")
    assert response.status_code == 200
    tracks = response.context["bans"]
    assert [t["title"] for t in tracks] == ["removed song", "New Song"]  # newest first
    assert tracks[0]["artist"] == "former artist" and tracks[0]["video_id"] is None
    assert tracks[0]["until_label"] == "Expired"
    assert tracks[1]["thumbnail"] == "/static/discovered-cover.jpg"
    assert "No expiry" in response.text
    assert 'href="https://music.youtube.com/watch?v=None"' not in response.text


def test_feedback_unban_preserves_quoted_track_keys(store):
    class UnbanButtons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button" and attrs.get("hx-post") == "/taste/unban":
                self.values.append(json.loads(attrs["hx-vals"]))

    key = '''song "live" & <rare>|artist's band'''
    store.rec_model.record_dislike(key, until=None, now=1.0)
    store.rec_model.record_dislike("other|artist", until=None, now=2.0)
    client = _client(store)
    buttons = UnbanButtons()
    buttons.feed(client.get("/taste/panels/feedback").text)
    assert {"key": key} in buttons.values
    response = client.post("/taste/unban", data={"key": key})
    assert response.status_code == 200 and response.headers["HX-Refresh"] == "true"
    assert store.rec_model.disliked_identity_keys() == {"other|artist"}


def test_feedback_summary_explains_signals_and_excludes_learning_markers(store):
    store.rec_model.record_like("liked|artist", now=1.0)
    store.rec_model.mark_graduated_once("liked|artist", "like_grad", now=1.0)
    store.rec_model.record_feedback("for_you", "later|artist", "not_now", until=100.0, now=1.0)
    store.rec_model.record_feedback("for_you", "artist:Band", "mute", now=1.0)
    html = _client(store).get("/taste/panels/feedback").text
    assert "Liked tracks" in html and "help the model learn what you enjoy" in html
    assert "Snoozed suggestions" in html and "when the snooze expires" in html
    assert "Muted artists or items" in html
    assert "like_grad" not in html and "not_now" not in html
    assert "Your trained model and saved taste weights are kept" in html
    assert "YouTube ratings stay as they are" in html


def test_taste_page_has_one_bar_of_section_tabs(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste").text
    assert 'aria-label="Taste Model sections"' in html
    assert html.count('role="tablist"') == 1
    assert html.index('id="taste-overview-tab"') < html.index('id="taste-experiment-tab"') < html.index('id="taste-profile-tab"')
    assert "Recommendations" in html
    assert "Model health" in html and "Feedback" in html
    assert "Recent activity" not in html
    html = c.get("/taste/panels/learning").text
    assert "Recent activity" in html
    assert "Learning progress" in html
    assert "how quickly recent influence fades" in html
    assert "At 100%, its saved preference gets a small update" in html
    assert "The four layers" not in html


def test_learning_tab_does_not_load_style_or_similarity_models(store, monkeypatch):
    from yt_playlist.rec import taste_viz

    def unexpected(*args, **kwargs):
        raise AssertionError("Learning must not load the other tabs' models")

    monkeypatch.setattr(taste_viz, "model_transparency", unexpected)
    monkeypatch.setattr(embed, "load_content_vectors", unexpected)
    monkeypatch.setattr(embed, "load_vectors", unexpected)
    store.rec_surface.bump_theme("artist:AC:DC", .6, 1.0)
    response = _client(store).get("/taste/panels/learning")
    assert response.status_code == 200
    assert "AC:DC" in response.text
    assert response.text.count('class="learning-card learning-card--') == 6


def test_model_health_kpis_render_without_loading_listening_models(store, monkeypatch):
    from yt_playlist.rec import taste_viz

    def unexpected(*args, **kwargs):
        raise AssertionError("Model KPIs must not load listening models")

    monkeypatch.setattr(taste_viz, "centroid_tilt_panel", unexpected)
    monkeypatch.setattr(taste_viz, "playlist_influence_panel", unexpected)
    monkeypatch.setattr(embed, "load_vectors", unexpected)
    store.settings.set_setting("rec_embed_method", "item2vec")
    store.settings.set_setting("rec_dim", "64")
    c = _client(store)
    for path in ("/taste/panels/health", "/taste/model-status"):
        r = c.get(path)
        assert r.status_code == 200
        assert "Tracks modeled" in r.text
        assert "Learning method" in r.text and "Item2Vec" in r.text
        assert "64<small> dimensions</small>" in r.text
        assert "model-health-details" not in r.text
        assert "Recent sound" not in r.text
        assert "Playlist influence" not in r.text


def test_taste_listening_readings_have_separate_destinations(store, monkeypatch):
    from yt_playlist.rec import taste_viz

    def unexpected(*args, **kwargs):
        raise AssertionError("This reading must not load the other reading's model")

    client = _client(store)
    with monkeypatch.context() as m:
        m.setattr(taste_viz, "playlist_influence_panel", unexpected)
        sound = client.get("/taste/viz/recent-sound")
        assert sound.status_code == 200
        assert "Not enough recent listening to compare sounds yet" in sound.text
    with monkeypatch.context() as m:
        m.setattr(taste_viz, "centroid_tilt_panel", unexpected)
        influence = client.get("/taste/viz/playlist-influence")
        assert influence.status_code == 200
        assert "Build the model and play some playlists" in influence.text

    profile = client.get("/taste/panels/profile").text
    learning = client.get("/taste/panels/learning").text
    health = client.get("/taste/panels/health").text
    assert 'hx-get="/taste/viz/playlist-influence"' in profile
    assert 'hx-get="/taste/viz/recent-sound"' not in profile
    assert 'hx-get="/taste/viz/recent-sound"' in learning
    assert 'hx-get="/taste/viz/playlist-influence"' not in learning
    assert "Learning method" in health and "Model size" in health
    assert "Recent sound" not in health and "Playlist influence" not in health
    assert "Track similarity model</h2>" not in health


def test_taste_page_shows_now_reading_with_live_posterior(store, monkeypatch):
    # #88: a live NOW posterior (seeded the same way test_now_layer.py does) renders as the NOW
    # ribbon on the Taste modes card - the only card on which the NOW layer exists.
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Warehouse techno", "families": [["techno", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    idx = {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.history.import_play_events(iid, rows)

    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                   base_url="http://127.0.0.1")
    html = c.get("/taste/panels/profile").text
    assert "Listening styles" in html
    assert "Warehouse techno" in html   # NOW/SESSION ribbon tooltip title
    assert "last 6h" in html            # NOW row's timescale (now_window_h default)
    assert "24h" in html and "influence halves every 4h" in html
    assert "100%" in html               # single-mode ribbon segment share, in the tooltip


def test_taste_page_hides_now_and_session_reading_when_quiet(store):
    # No modes, no recent plays -> below the confidence gate. The card stays, the fast rows go quiet.
    html = _client(store).get("/taste/panels/profile").text
    assert "Listening styles" in html
    assert "No listening styles found yet" in html


def test_taste_page_draws_the_full_chain_on_a_facet_axis(store):
    # #88: wherever the page contrasts permanent and transient it must draw every layer that exists
    # on that axis - here, the whole multiplier chain, not just a share and a shrug.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("vj", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Jazz")
    store.history.add_history_snapshot(iid, 1.0, ["song|band"])
    html = _client(store).get("/taste/panels/profile").text
    assert "Genre families" in html
    for cap in ("What you play", "Saved", "Recent", "Combined"):
        assert cap in html
    assert "Recent multiplier" in html
    assert "= Combined multiplier" in html
