from fastapi.testclient import TestClient

from yt_playlist import embed
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
