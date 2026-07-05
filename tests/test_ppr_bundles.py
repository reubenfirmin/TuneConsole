import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import embed, recommend, surfaces, ppr, mode_surfaces as ms


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


class _Item:
    def __init__(self, key):
        self.key = key; self.video_id = "v" + key; self.title = "T" + key; self.artist = "A" + key
        self.album = ""; self.thumbnail = None; self.plays = 0; self.reason = ""; self.lane = ""
        self.genre = "house"


def test_prepare_stashes_ppr_order(monkeypatch, store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    keys = ["k1", "k2"]
    V = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    idx = {"k1": 0, "k2": 1}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    monkeypatch.setattr(embed, "load_discovered_content_vectors", lambda s: ([], None, {}))
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(recommend, fn, lambda s, n, limit=0: [_Item("k1"), _Item("k2")])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None: [])
    # Stub the PPR ranking so the assertion is exact and independent of the graph internals.
    monkeypatch.setattr(ppr, "mode_rankings", lambda s: {1: ["k2", "k1"]})

    payload = ms.prepare_bundles(store, now=10.0)
    assert payload["_ppr"] == {"1": ["k2", "k1"]}
    assert store.get_proposals("mode_bundles")["_ppr"] == {"1": ["k2", "k1"]}


def test_prepare_ppr_failopen(monkeypatch, store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (["k1"], np.array([[1.0, 0.0]], dtype=np.float32), {"k1": 0}))
    monkeypatch.setattr(embed, "load_discovered_content_vectors", lambda s: ([], None, {}))
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(recommend, fn, lambda s, n, limit=0: [])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None: [])

    def _boom(s):
        raise RuntimeError("ppr blew up")
    monkeypatch.setattr(ppr, "mode_rankings", _boom)

    payload = ms.prepare_bundles(store, now=10.0)      # must not raise
    assert payload["_ppr"] == {}
