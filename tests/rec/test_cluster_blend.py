import numpy as np
from yt_playlist.rec import embed, rec_params


class VStore:
    """Minimal store stub: serves collaborative + content vectors and the knob setting."""
    def __init__(self, collab, content, w=0.30):
        self._collab = collab      # {key: np.array}
        self._content = content     # {key: np.array}
        self._settings = {rec_params.SETTING_PREFIX + "cluster_content_weight": str(w)}

    def get_rec_vectors(self):
        return [(k, v.astype(np.float32).tobytes()) for k, v in self._collab.items()]

    def get_rec_content_vectors(self):
        return [(k, v.astype(np.float32).tobytes()) for k, v in self._content.items()]

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


def _unit(*xs):
    v = np.array(xs, dtype=np.float64); return v / (np.linalg.norm(v) + 1e-9)


def test_content_blend_pulls_same_genre_non_cooccurring_track():
    # Collaborative space: seed S sits near BRIT (cos≈0.9), far from PSY (cos≈0.1 — they rarely
    # co-occur in playlists). Content space: S and PSY share a genre (cos≈0.99); BRIT is a
    # different genre (cos≈0.1). At w=0.6 the content pull should flip PSY above BRIT.
    collab = {"S": _unit(1, 0), "BRIT": _unit(0.9, 0.436), "PSY": _unit(0.1, 0.995)}
    content = {"S": _unit(1, 0), "PSY": _unit(0.99, 0.141), "BRIT": _unit(0.1, 0.995)}
    # With pure collaborative (w=0) BRIT outranks PSY; with content blend PSY should rise.
    base = embed.cluster_expand(VStore(collab, content, w=0.0), pos_keys=["S"], topn=2)
    blended = embed.cluster_expand(VStore(collab, content, w=0.6), pos_keys=["S"], topn=2)
    base_order = [k for k, _ in base]
    blended_order = [k for k, _ in blended]
    assert base_order[0] == "BRIT"                 # old behaviour: co-occurrence wins
    assert blended_order[0] == "PSY"               # blended: musical similarity reaches PSY


def test_w_zero_matches_collaborative_only():
    collab = {"S": _unit(1, 0), "A": _unit(0.9, 0.1), "B": _unit(0.2, 0.9)}
    content = {"S": _unit(1, 0), "A": _unit(0, 1), "B": _unit(1, 0)}
    only_collab = embed.cluster_expand(VStore(collab, {}, w=0.0), pos_keys=["S"], topn=2)
    w0_with_content = embed.cluster_expand(VStore(collab, content, w=0.0), pos_keys=["S"], topn=2)
    assert [k for k, _ in only_collab] == [k for k, _ in w0_with_content]


def test_candidate_without_content_vector_falls_back_to_collab():
    collab = {"S": _unit(1, 0), "A": _unit(0.9, 0.1)}
    content = {"S": _unit(1, 0)}     # A has NO content vector
    out = embed.cluster_expand(VStore(collab, content, w=0.6), pos_keys=["S"], topn=1)
    assert out and out[0][0] == "A"   # A still returned, scored on collaborative alone


def test_diverse_seeds_surface_each_genre_not_the_blend():
    """With two DISSIMILAR seeds (R rock-ish at angle 0, P psy-ish at angle 90), seed-fanout should
    rank tracks near EITHER seed (A near R, B near P) above a track that merely sits at the averaged
    midpoint (M) — so a minority seed reaches its own genre instead of being averaged away."""
    R, P = _unit(1, 0), _unit(0, 1)
    A, B, M = _unit(0.95, 0.31), _unit(0.31, 0.95), _unit(0.707, 0.707)
    collab = {"R": R, "P": P, "A": A, "B": B, "M": M}
    out = embed.cluster_expand(VStore(collab, {}, w=0.0), pos_keys=["R", "P"], topn=3)
    order = [k for k, _ in out]
    assert order[:2] == ["A", "B"] or order[:2] == ["B", "A"]   # on-genre tracks lead
    assert order[2] == "M"                                       # the averaged-blend track is demoted


def test_single_seed_is_unchanged_by_fanout():
    """With one seed, fanout is a no-op (centroid == that seed), so focused grows are unaffected."""
    collab = {"S": _unit(1, 0), "A": _unit(0.95, 0.31), "B": _unit(0.2, 0.98)}
    out = embed.cluster_expand(VStore(collab, {}, w=0.0), pos_keys=["S"], topn=2)
    assert [k for k, _ in out] == ["A", "B"]   # nearest-to-the-single-seed order, as before
