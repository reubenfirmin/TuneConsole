import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import embed, recommend, surfaces, mode_surfaces as ms


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


class _Item:
    def __init__(self, key, score):
        self.key = key; self.video_id = "v"+key; self.title = "T"+key; self.artist = "A"
        self.album = ""; self.thumbnail = None; self.plays = 0; self.reason = ""; self.lane = ""
        self.genre = "house"; self._score = score


def test_prepare_buckets_by_nearest_mode(monkeypatch, store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
        {"mode_id": 2, "label": "b", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    keys = ["k1", "k2"]
    V = np.array([[1.0, 0.05], [0.05, 1.0]], dtype=np.float32)
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    idx = {"k1": 0, "k2": 1}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    monkeypatch.setattr(embed, "load_discovered_content_vectors", lambda s: ([], None, {}))
    pool = [_Item("k1", 0.9), _Item("k2", 0.8)]
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(recommend, fn, lambda s, n, limit=0, _p=pool: list(_p))
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None: [])

    payload = ms.prepare_bundles(store, now=10.0)
    assert [d["key"] for d in payload["1"]["wheelhouse"]] == ["k1"]
    assert [d["key"] for d in payload["2"]["wheelhouse"]] == ["k2"]
    assert store.get_proposals("mode_bundles") is not None


def test_temporal_excludes_disliked_library_tracks(monkeypatch, store):
    """The temporal surface is built straight from library content-vector membership, so it must still
    honour YouTube dislikes (suppressed_keys) the way the scorer pools do, or a thumbs-down track leaks
    into the Throwback card and into the generated playlist."""
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    keys = ["keep", "hated"]
    V = np.array([[1.0, 0.0], [0.95, 0.05]], dtype=np.float32)
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    idx = {"keep": 0, "hated": 1}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    monkeypatch.setattr(embed, "load_discovered_content_vectors", lambda s: ([], None, {}))
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(recommend, fn, lambda s, n, limit=0: [])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None: [])
    store.record_dislike("hated", until=None, now=10.0)

    payload = ms.prepare_bundles(store, now=10.0)
    temporal_keys = {d["key"] for d in payload["1"]["temporal"]}
    all_temporal_keys = {d["key"] for d in payload["all"]["temporal"]}
    assert "keep" in temporal_keys
    assert "hated" not in temporal_keys
    assert "hated" not in all_temporal_keys


def test_prepare_empty_when_no_modes(store):
    assert ms.prepare_bundles(store, now=10.0) == {}
    assert store.get_proposals("mode_bundles") == {}


def test_rebuild_wires_prepare_bundles_and_drops_fresh_songs():
    import inspect
    from yt_playlist.rec import rec_worker
    src = inspect.getsource(rec_worker.RecWorker._do_rebuild)
    assert "mode_surfaces.prepare_bundles" in src
    assert "try:" in src and src.index("try:") < src.index("mode_surfaces.prepare_bundles")
    assert "fresh_songs" not in src
