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


def _client(store, now=10_000.0):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient()
    return TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def test_taste_page_lists_modes(store):
    store.conn.executescript(
        "INSERT INTO tracks (identity_key, title, artist, genre) VALUES "
        "('h|1', 'Strobe', 'Deadmau5', 'house'),"
        "('h|2', 'Opus', 'Eric Prydz', 'house');")
    store.conn.commit()
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "house", "families": [["house", 2]],
          "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 2,
          "rep_keys": ["h|1", "h|2"]}], retired_ids=[], now=9_000.0)
    c = _client(store)
    r = c.get("/taste")
    assert r.status_code == 200
    assert "house" in r.text
    assert "Strobe" in r.text and "Opus" in r.text


def test_taste_page_empty_modes_state(store):
    c = _client(store)
    r = c.get("/taste")
    assert r.status_code == 200
    assert "No taste modes yet" in r.text
