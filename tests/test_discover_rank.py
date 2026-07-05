"""#86 discovery ranking: bridge strength ranks; proxy taste only gates. The old taste*strength
product double-counted anchors (the proxy IS the anchors' weighted average)."""
import numpy as np

from yt_playlist.rec.discover import _rank_candidates


class _PT:
    """Stub playlist-taste: score(vec) is positive iff the vector points at +x."""
    def score(self, vec):
        v = vec / (np.linalg.norm(vec) + 1e-9)
        return float(v[0]), [("Playlist A", 0.5)]


def _bridge(weight, direction):
    v = np.array(direction, dtype=np.float64)
    return (v / np.linalg.norm(v), weight, "anchor")


def test_score_is_strength_not_taste_times_strength():
    pt = _PT()
    # One strong on-taste edge vs two weaker edges summing higher: strength must decide.
    bridges = {"weak_single": [_bridge(1.0, [1, 0])],
               "strong_multi": [_bridge(0.8, [1, 0]), _bridge(0.7, [1, 0.1])]}
    out = {c["cand"]: c for c in _rank_candidates(bridges, pt, 2)}
    assert out["strong_multi"]["score"] > out["weak_single"]["score"]
    assert out["weak_single"]["score"] == 1.0          # strength, no taste factor


def test_off_taste_candidate_is_gated_out():
    pt = _PT()
    bridges = {"off": [_bridge(5.0, [-1, 0])]}          # points away from taste
    assert _rank_candidates(bridges, pt, 2) == []


def test_because_names_top_anchors_and_fits_pass_through():
    pt = _PT()
    v = np.array([1.0, 0.0])
    bridges = {"c": [(v, 0.2, "minor"), (v, 0.9, "major")]}
    (item,) = _rank_candidates(bridges, pt, 2)
    assert item["because"][0] == "major" and item["fits"] == ["Playlist A"]
