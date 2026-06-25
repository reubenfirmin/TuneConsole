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
    assert store.get_weights()["lane:deep_cut"] == 0.5
    assert c.post("/taste/reset-weights").status_code == 200
    assert store.get_weights() == {}
    store.record_feedback("for_you", "a|b", "dismiss", now=1.0)
    assert c.post("/taste/clear-feedback").status_code == 200
    assert store.suppressed_keys("for_you", now=2.0) == set()


def test_taste_recall_fragment(store):
    c = _client(store)
    assert c.get("/taste/recall").status_code == 200   # no vectors -> "build first", still 200


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
    assert store.get_weights()["genre:rock"] == 0.0


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
