import numpy as np
from yt_playlist.rec import taste_modes as tm


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _disc(vec, label="x"):
    return {"centroid": _unit(vec), "size": 50, "families": [(label, 50)], "rep_keys": [], "label": label}


def _exist(mid, vec):
    return {"mode_id": mid, "centroid": _unit(vec)}


def test_close_centroid_keeps_id():
    existing = [_exist(7, [1, 0, 0])]
    discovered = [_disc([0.99, 0.14, 0])]   # cos ~ 0.99
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] == 7
    assert retired == []


def test_far_centroid_is_new():
    existing = [_exist(7, [1, 0, 0])]
    discovered = [_disc([0, 1, 0])]         # cos 0
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] is None
    assert retired == [7]


def test_below_threshold_does_not_reuse_id():
    existing = [_exist(7, [1, 0, 0])]
    discovered = [_disc([0.5, 0.866, 0])]   # cos 0.5 < 0.6
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] is None and retired == [7]


def test_greedy_best_match_wins():
    existing = [_exist(1, [1, 0, 0]), _exist(2, [0, 1, 0])]
    discovered = [_disc([0, 1, 0], "b"), _disc([1, 0, 0], "a")]
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    ids = {u["label"]: u["mode_id"] for u in upserts}
    assert ids == {"a": 1, "b": 2} and retired == []
