"""Autotune reports, recommendation previews and baseline comparisons."""
import json
from tests.taste_support import _client

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
    _set_autotune_result(store, metric="recall_at_k", in_sample=True)
    client = _client(store)
    main = client.get("/taste/autotune-status").text
    assert "Earlier manual Auto-tune result" not in main
    assert '<details' not in main
    assert 'href="/taste/model-selection/history"' in main
    html = client.get("/taste/model-selection/history").text
    assert "Playlist patterns (SVD)" in html
    assert "Selected model" in html and "Previous model" in html
    assert "percentage points" in html
    # Historical reports must retain the original evaluation's in-sample warning.
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


def test_taste_preview_renders_without_recs(store):
    c = _client(store)
    assert c.get("/taste/preview").status_code == 200     # no model yet -> still 200


def test_taste_preview_explains_scope_sources_and_two_different_samples(store):
    html = _client(store).get("/taste/panels/experiment").text
    preview = html.split('id="taste-preview"', 1)[1]
    for text in ("Preview &amp; compare", "Home’s familiar-music mix", "Taste matches",
                 "Deep cuts", "Playlist neighbors", "recent-listening influence and memory",
                 "Source weights in Listening choices", "Up to 8 songs", "top 24 picks"):
        assert text in preview
    for destination in ("#recommendations/main", "#recommendations/genres", "#recommendations/features",
                        "#learning", "#generated"):
        assert f'href="{destination}"' in preview


def test_taste_preview_explains_production_score_trace_in_plain_language(store, monkeypatch):
    from yt_playlist.rec import surfaces
    trace = {"durable": 0.4, "transient_delta": 0.1, "session_delta": -0.02,
             "audio_delta": 0.03, "negative_delta": -0.05, "combined": 0.46, "rank_base": 0.8,
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
    assert "It resembles music you disliked" in html
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


