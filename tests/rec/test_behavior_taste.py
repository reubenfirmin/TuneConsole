"""#38 behavior-oriented taste: the tracks you actually play form a co-equal context in playlist_taste
(via _behavior_centroid), so taste leans on listening, not only playlist curation."""
import numpy as np

from yt_playlist.rec import scoring
from yt_playlist.rec.scoring import PlaylistTaste, _behavior_centroid, BEHAVIOR_TASTE_W


def test_behavior_centroid_from_played_tracks(monkeypatch, store):
    M = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]], dtype=np.float32)
    idx = {"a|x": 0, "b|y": 1, "c|z": 2}
    monkeypatch.setattr(store, "play_counts", lambda: {"a|x": 5, "b|y": 1}, raising=False)
    c = _behavior_centroid(store, M, idx)
    assert c is not None and abs(np.linalg.norm(c) - 1.0) < 1e-6
    assert c[0] > 0 and c[1] > 0                       # blends the two played tracks (c|z unplayed, excluded)


def test_behavior_centroid_none_when_nothing_played(monkeypatch, store):
    monkeypatch.setattr(store, "play_counts", lambda: {}, raising=False)
    assert _behavior_centroid(store, np.eye(2, dtype=np.float32), {"a|x": 0, "b|y": 1}) is None


def test_playlist_taste_appends_behavior_context(monkeypatch, store):
    # one playlist context + plays -> taste gains a co-equal "Your listening" context at BEHAVIOR_TASTE_W
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    monkeypatch.setattr(scoring.embed, "load_vectors", lambda s: (["a|x", "b|y"], V, {"a|x": 0, "b|y": 1}))
    monkeypatch.setattr(scoring, "_playlist_centroids",
                        lambda s, M, idx: PlaylistTaste(["P"], np.array([[1.0, 0.0]]), np.array([1.0]), [7]))
    monkeypatch.setattr(store, "play_counts", lambda: {"b|y": 3}, raising=False)
    pt = scoring.playlist_taste(store)
    assert pt.titles == ["P", "Your listening"]
    assert abs(pt.weights[-1] - BEHAVIOR_TASTE_W) < 1e-9
    assert abs(pt.weights.sum() - 1.0) < 1e-9          # weights still normalized
