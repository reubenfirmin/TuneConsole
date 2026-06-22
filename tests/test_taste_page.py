from fastapi.testclient import TestClient

from yt_playlist import embed, rec_params
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
    assert "/taste/rebuild" in html


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
