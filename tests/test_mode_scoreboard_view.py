import numpy as np
import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _client(store, now=1000.0):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def test_taste_shows_offered_and_picked(store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "house", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    store.modes.log_impressions(1, [("wheelhouse", 1)], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=110.0)
    store.modes.log_pick(playlist_id=5, mode_id=1, now=120.0)
    r = _client(store).get("/taste")
    assert r.status_code == 200
    assert "house" in r.text
    assert "offered" in r.text.lower()
