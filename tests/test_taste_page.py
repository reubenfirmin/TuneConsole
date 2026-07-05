import numpy as np
from fastapi.testclient import TestClient

from yt_playlist.rec import embed, rec_params
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1")


def test_taste_page_renders_status_and_controls(store):
    c = _client(store)
    html = c.get("/taste").text
    assert "Taste Model" in html
    assert "taste breadth" in html
    assert 'name="weight"' in html            # editable blend weights
    assert "/taste/autotune" in html          # auto-tune control present


def test_autotune_route_starts_and_reports(store):
    c = _client(store)
    # POST kicks off a background run and returns a polling fragment (not an HX-Refresh).
    r = c.post("/taste/autotune")
    assert r.status_code == 200
    assert "HX-Refresh" not in r.headers
    assert "autotune-status" in r.text  # self-poll wired up
    # Status endpoint is reachable and renders the fragment.
    s = c.get("/taste/autotune-status")
    assert s.status_code == 200


def test_taste_page_shows_transient_and_graduation_controls(store):
    html = _client(store).get("/taste").text
    assert "Right-now responsiveness" in html
    assert "Learning" in html
    assert 'id="param-graduation_enabled"' in html   # the toggle rendered
    assert 'id="param-play_transient_w"' in html


def test_autotune_done_status_refreshes_model_status(store):
    c = _client(store)
    # The completed (idle) status poll fires the event the Model status card listens for.
    s = c.get("/taste/autotune-status")
    assert s.headers.get("HX-Trigger") == "autotune-done"
    # The Model status fragment renders and re-arms its own auto-refresh listener.
    m = c.get("/taste/model-status")
    assert m.status_code == 200
    assert 'id="model-status"' in m.text
    assert "track vectors" in m.text
    # The page wires the card to listen for autotune-done.
    page = c.get("/taste").text
    assert "autotune-done from:body" in page


def test_taste_page_empty_autotune_state(store):
    html = _client(store).get("/taste").text
    assert "Auto-tune hasn't run yet" in html      # empty-state note before any run


def test_taste_page_renders_autotune_result_panel(store):
    from yt_playlist.rec import autotune_run
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(40)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(40)]
    for j in range(6):
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PA{j}", "PA", 8, f"ha{j}", 0.0), A[j*5:j*5+8])
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PB{j}", "PB", 8, f"hb{j}", 0.0), B[j*5:j*5+8])
    autotune_run.run_and_record(store, now=1000.0)
    html = _client(store).get("/taste").text
    assert "chosen config" in html                 # the populated result panel rendered
    assert "recall@20 (winner)" in html
    # No history was recorded, so the sweep judged itself in-sample: the panel must say so.
    assert "Tuned on in-sample recall" in html


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
    store.set_setting(autotune_run.RESULT_SETTING, json.dumps(payload))


def test_taste_page_autotune_temporal_result_has_no_in_sample_warning(store):
    _set_autotune_result(store)
    html = _client(store).get("/taste").text
    assert "temporal recall@20" in html             # metric is named, not a bare "recall@20"
    assert "Tuned on in-sample recall" not in html
    assert "Sweep failed" not in html


def test_taste_page_autotune_sweep_failed_renders_line(store):
    _set_autotune_result(store, metric="recall_at_k", in_sample=True, sweep_failed=True,
                          winner={"method": "svd", "dim": 48, "recall": 0.2, "metric": "recall_at_k"},
                          previous={"method": "svd", "dim": 48, "recall": 0.2, "metric": "recall_at_k"})
    html = _client(store).get("/taste").text
    assert "Sweep failed: kept the previous configuration" in html


def test_taste_page_uses_sliders_not_number_fields(store):
    iid = store.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste").text
    assert 'type="range"' in html                 # knobs are sliders now
    assert 'type="number"' not in html            # ...not typed number fields
    assert "/taste/param" in html                 # scalar knobs post here
    assert "/taste/reset-all" in html             # global reset at the top
    assert "/taste/preview" in html               # manual-refresh live sample
    assert "Blend weights" in html and "Genre families" in html
    assert "Advanced" in html                     # collapsed advanced disclosure


def test_taste_page_renders_genre_slider_for_tagged_library(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Techno")
    store.add_history_snapshot(iid, 1.0, ["song|band"])    # play it so it counts toward the mix
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste").text
    assert 'genre:techno' in html                 # a per-genre-family weight control is rendered


def test_taste_controls_round_trip(store):
    c = _client(store)
    assert c.post("/taste/weight", data={"axis": "lane:deep_cut", "weight": "0.5"}).status_code == 200
    assert store.get_weights(now=1.0)["lane:deep_cut"] == 0.5
    assert c.post("/taste/reset-weights").status_code == 200
    assert store.get_weights() == {}
    store.record_feedback("for_you", "a|b", "dismiss", now=1.0)
    assert c.post("/taste/clear-feedback").status_code == 200
    assert store.suppressed_keys("for_you", now=2.0) == set()


def test_taste_recall_fragment(store):
    c = _client(store)
    r = c.get("/taste/recall")
    assert r.status_code == 200   # no vectors -> "build first", still 200
    assert "holdout: 1d" in r.text   # temporal panel names the actual holdout window used


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
    assert store.get_weights(now=1.0)["genre:rock"] == 0.0


def test_taste_reset_param_restores_default(store):
    c = _client(store)
    rec_params.set_param(store, "comfort_min_plays", 3)
    assert c.post("/taste/reset-param", data={"name": "comfort_min_plays"}).status_code == 200
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_taste_reset_all_clears_weights_and_params(store):
    c = _client(store)
    store.set_weight("lane:deep_cut", 0.5)
    rec_params.set_param(store, "comfort_min_plays", 3)
    assert c.post("/taste/reset-all").status_code == 200
    assert store.get_weights() == {}
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_taste_preview_renders_without_recs(store):
    c = _client(store)
    assert c.get("/taste/preview").status_code == 200     # no model yet -> still 200


def test_taste_page_lists_and_clears_bans(store):
    store.record_dislike("song|band", until=99999.0, now=1.0)
    c = _client(store)
    html = c.get("/taste").text
    assert "song|band" in html or "Banned" in html
    assert c.post("/taste/unban", data={"key": "song|band"}).status_code == 200
    assert store.disliked_identity_keys() == set()


def test_taste_page_has_viz_and_controls_tabs(store):
    iid = store.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste").text
    assert "Visualizations" in html and "Controls" in html
    assert "tab = 'viz'" in html                  # default viz tab via the setup Alpine pattern
    assert "What's feeding" in html               # transient sources panel present
    assert "Graduation funnel" in html            # transient -> permanent funnel present


def test_taste_viz_engine_fragment(store):
    iid = store.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    r = c.get("/taste/viz/engine")
    assert r.status_code == 200
    assert "track vectors" in r.text


def test_taste_page_shows_now_reading_with_live_posterior(store, monkeypatch):
    # #88: a live NOW posterior (seeded the same way test_now_layer.py does) renders in the new
    # "Layer stack" card's NOW row - ribbon + tooltip naming the mode, instead of the old one-line
    # "Right now: ..." paragraph (which this card subsumes).
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Warehouse techno", "families": [["techno", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    idx = {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    iid = store.upsert_identity("main", "cred", None, True)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.import_play_events(iid, rows)

    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                   base_url="http://127.0.0.1")
    html = c.get("/taste").text
    assert "Layer stack" in html
    assert "Warehouse techno" in html   # NOW/SESSION ribbon tooltip title
    assert "last 6h" in html            # NOW row's timescale (now_window_h default)
    assert "24h" in html and "4h half-life" in html   # SESSION row's timescale
    assert "100.0%" in html             # single-mode ribbon segment share, in the tooltip


def test_taste_page_hides_now_and_session_reading_when_quiet(store):
    # No modes, no recent plays -> below the confidence gate -> the honest quiet copy for both rows.
    html = _client(store).get("/taste").text
    assert "Layer stack" in html
    assert "Quiet: fewer than" in html
    assert "plays with a known sound in the window." in html
    assert "plays with a known sound in the last 24h." in html
