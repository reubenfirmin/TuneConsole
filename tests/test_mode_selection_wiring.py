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


def _seed_bundles(store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
        {"mode_id": 2, "label": "b", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 60, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    from yt_playlist.rec import mode_surfaces as ms
    def items(p, g):
        return [{"key": f"{p}{i}", "video_id": f"v{p}{i}", "title": f"Song {p}{i}", "artist": f"Art {p}{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": g}
                for i in range(20)]
    payload = {"1": {}, "2": {}}
    for surf in ms.CARD_SURFACES:
        payload["1"][surf] = items(f"{surf}h", "house")
        payload["2"][surf] = items(f"{surf}t", "techno")
    store.put_proposals("mode_bundles", payload, 1.0)


def _client(store, now=1000.0):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def test_cards_render_logs_impressions(store):
    _seed_bundles(store)
    r = _client(store).get("/home/cards")
    assert r.status_code == 200
    assert sum(store.modes.impression_counts().values()) >= 1
    assert "mode_id" in r.text       # the recipe carrying mode_id is embedded for Save & play


def test_generate_logs_pick(store):
    _seed_bundles(store)
    r = _client(store).post("/home/generate", data={
        "name": "My Mix",
        "tracks": '[{"video_id": "vX", "title": "T", "artist": "A"}]',
        "recipe": '{"model": "mode", "mode_id": 2}'})
    assert r.status_code == 200
    rows = store.modes.pick_rows()
    assert len(rows) == 1 and rows[0][1] == 2


def test_generate_no_pick_without_mode(store):
    _seed_bundles(store)
    _client(store).post("/home/generate", data={
        "name": "M", "tracks": '[{"video_id": "vX", "title": "T", "artist": "A"}]',
        "recipe": 'null'})
    assert store.modes.pick_rows() == []
