import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import embedding_vectors as embed, surfaces, mode_surfaces as ms


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
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: (keys, V, idx))
    monkeypatch.setattr(embed, "load_discovered_content_vectors", lambda s, **kw: ([], None, {}))
    pool = [_Item("k1", 0.9), _Item("k2", 0.8)]
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit=0, _p=pool: list(_p))
    fresh = surfaces.ForYouItem("New song", "New artist", "", "new", None, 0, "", key="new|artist",
                                genre="house", year=2026)
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None: [fresh])
    store.rec_proposals.put_proposals("fresh_pool", [{"key": "stale|artist", "genre": "techno"}], 1)

    payload = ms.prepare_bundles(store, now=10.0)
    assert [d["key"] for d in payload["1"]["wheelhouse"]] == ["k1"]
    assert [d["key"] for d in payload["2"]["wheelhouse"]] == ["k2"]
    assert payload["all"]["fresh"][0]["year"] == 2026
    assert store.rec_proposals.get_proposals("mode_bundles") is not None
    refreshed = store.rec_proposals.get_proposals("fresh_pool")
    assert len(refreshed) == 1 and refreshed[0]["key"] == "new|artist"
    assert refreshed[0]["genre"] == "house" and refreshed[0]["year"] == 2026
    assert refreshed[0]["score"] is None
    assert store.conn.execute("SELECT configuration FROM rec_serving_policies WHERE model_version=?",
                              (refreshed[0]["pool_policy"],)).fetchone() is not None


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
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: (keys, V, idx))
    monkeypatch.setattr(embed, "load_discovered_content_vectors", lambda s, **kw: ([], None, {}))
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit=0: [])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None: [])
    store.rec_model.record_dislike("hated", until=None, now=10.0)

    payload = ms.prepare_bundles(store, now=10.0)
    temporal_keys = {d["key"] for d in payload["1"]["temporal"]}
    all_temporal_keys = {d["key"] for d in payload["all"]["temporal"]}
    assert "keep" in temporal_keys
    assert "hated" not in temporal_keys
    assert "hated" not in all_temporal_keys


def test_prepare_empty_when_no_modes(store):
    assert ms.prepare_bundles(store, now=10.0) == {}
    assert store.rec_proposals.get_proposals("mode_bundles") == {}


def test_fresh_refresh_discards_pools_without_live_models_or_candidates(store):
    from yt_playlist.rec import fresh_pool

    old = [{"key": "stale", "artist": "Artist", "genre": "house"}]
    library = [{"key": "owned", "artist": "Library artist", "genre": "house"}]
    store.rec_proposals.put_proposals("fresh_pool", old, 1)
    store.rec_proposals.put_proposals("mode_bundles", {
        "1": {"fresh": old, "wheelhouse": library},
        "all": {"fresh": old, "wheelhouse": library},
        "_meta": {"full_pools": True}, "_ppr": {"1": ["owned"]},
    }, 1)

    fresh_pool.prepare(store, 10)  # No discoveries remain.

    assert fresh_pool.pick(store, 10) == []
    assert store.rec_proposals.get_proposals("mode_bundles") == {}
    assert store.rec_proposals.get_proposals("fresh_pool") == []


def test_prepare_reaches_genres_below_the_old_global_pool_cutoff(monkeypatch, store):
    store.modes.replace_modes([
        {"mode_id": m, "label": f"m{m}", "families": [["house", 1]],
         "centroid": np.eye(2, dtype=np.float32)[m - 1], "size": 50, "rep_keys": []}
        for m in (1, 2)], retired_ids=[], now=1.0)
    keys = [f"k{i}" for i in range(500)]
    vectors = np.array([[1., 0.]] * 450 + [[0., 1.]] * 50, dtype=np.float32)
    monkeypatch.setattr(embed, "load_content_vectors",
                        lambda s, **kw: (keys, vectors, {key: i for i, key in enumerate(keys)}))
    monkeypatch.setattr(embed, "load_discovered_content_vectors", lambda s, **kw: ([], None, {}))
    pool = [_Item(key, 1.0) for key in keys]
    for fn in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, fn, lambda s, n, limit: pool[:limit])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda s, n, limit=None: [])

    payload = ms.prepare_bundles(store, now=10.0)

    assert len(payload["2"]["wheelhouse"]) >= ms.PROTO_SIZE
    assert len(payload["all"]["wheelhouse"]) == 500
    assert len(payload["all"]["temporal"]) == 500
