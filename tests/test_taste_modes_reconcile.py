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


# --- content-space guard -------------------------------------------------------------------------
# The content space is rebuilt whenever enrichment introduces a new genre/key token, which both grows
# the dimension and can shift what a column means. A centroid persisted in the old space must never be
# scored against a fresh one: doing so crashed the (best-effort, so silently swallowed) rebuild with
# "matmul: Input operand 1 has a mismatch in its core dimension", and in the same-dimension case would
# have matched modes through a reordered basis with no error at all.

def _disc_s(vec, space, label="x"):
    d = _disc(vec, label)
    d["space"] = space
    return d


def _exist_s(mid, vec, space):
    e = _exist(mid, vec)
    e["space"] = space
    return e


def test_stale_space_centroid_of_different_dim_does_not_crash():
    existing = [_exist_s(7, [1, 0, 0], "old")]          # 3 dims, old space
    discovered = [_disc_s([1, 0, 0, 0], "new")]         # 4 dims, new space
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] is None                # fresh id, not a bogus match
    assert retired == [7]                               # the un-comparable mode retires


def test_same_dim_different_space_is_not_matched():
    """The dangerous case: dims agree, so a matmul would succeed and silently lie."""
    existing = [_exist_s(7, [1, 0, 0], "old")]
    discovered = [_disc_s([1, 0, 0], "new")]            # identical vector, different basis
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] is None
    assert retired == [7]


def test_same_space_still_matches():
    existing = [_exist_s(7, [1, 0, 0], "same")]
    discovered = [_disc_s([0.99, 0.14, 0], "same")]
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] == 7 and retired == []


def test_migrated_rows_with_empty_space_retire_against_a_real_space():
    """Pre-migration rows carry space=''; they must not match a fingerprinted discovery."""
    existing = [_exist_s(7, [1, 0, 0], "")]
    discovered = [_disc_s([1, 0, 0], "abc123")]
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] is None and retired == [7]


# --- cross-space identity via representative tracks ----------------------------------------------
# A centroid cannot cross a content-space rebuild, but rep_keys are identity_keys: stable strings that
# do not depend on the embedding basis. Two modes from different spaces that share most of their
# representative tracks ARE the same taste region, and must keep the same mode_id, or every pick,
# impression, and Thompson posterior attached to that id is silently discarded
# (see rec/mode_eval.mode_bandit_stats -> rec/mode_surfaces.thompson_mode_scores).

def _disc_r(vec, space, reps, label="x"):
    d = _disc(vec, label)
    d["space"] = space
    d["rep_keys"] = reps
    return d


def _exist_r(mid, vec, space, reps):
    e = _exist(mid, vec)
    e["space"] = space
    e["rep_keys"] = reps
    return e


def test_cross_space_modes_keep_their_id_when_representatives_overlap():
    existing = [_exist_r(7, [1, 0, 0], "old", ["t1", "t2", "t3", "t4"])]
    discovered = [_disc_r([1, 0, 0, 0], "new", ["t1", "t2", "t3", "t9"])]   # 3 shared of 5 union
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] == 7
    assert retired == []


def test_cross_space_modes_with_disjoint_representatives_still_retire():
    existing = [_exist_r(7, [1, 0, 0], "old", ["t1", "t2", "t3"])]
    discovered = [_disc_r([1, 0, 0, 0], "new", ["z1", "z2", "z3"])]
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] is None
    assert retired == [7]


def test_cross_space_match_is_greedy_and_deterministic():
    """Best overlap wins, and an existing mode is claimed at most once."""
    existing = [_exist_r(1, [1, 0, 0], "old", ["a", "b", "c", "d"]),
                _exist_r(2, [0, 1, 0], "old", ["w", "x", "y", "z"])]
    discovered = [_disc_r([0, 1, 0, 0], "new", ["w", "x", "y", "q"], "B"),
                  _disc_r([1, 0, 0, 0], "new", ["a", "b", "c", "q"], "A")]
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    ids = {u["label"]: u["mode_id"] for u in upserts}
    assert ids == {"A": 1, "B": 2} and retired == []


def test_same_space_still_prefers_centroid_cosine():
    """Within one space the centroid is the better evidence; rep overlap must not override it."""
    existing = [_exist_r(7, [1, 0, 0], "same", ["t1", "t2", "t3"])]
    discovered = [_disc_r([0.99, 0.14, 0], "same", ["z9"])]     # no rep overlap, high cosine
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] == 7 and retired == []


def test_a_same_space_match_outranks_a_cross_space_one():
    """Two candidates for one existing mode: the same-space centroid match must win the id."""
    existing = [_exist_r(7, [1, 0, 0], "same", ["t1", "t2", "t3"])]
    discovered = [_disc_r([1, 0, 0, 0], "new", ["t1", "t2", "t3"], "crossspace"),
                  _disc_r([0.99, 0.14, 0], "same", ["z9"], "samespace")]
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    ids = {u["label"]: u["mode_id"] for u in upserts}
    assert ids["samespace"] == 7
    assert ids["crossspace"] is None
    assert retired == []


def test_empty_rep_keys_never_match_across_spaces():
    existing = [_exist_r(7, [1, 0, 0], "old", [])]
    discovered = [_disc_r([1, 0, 0, 0], "new", [])]
    upserts, retired = tm.reconcile(existing, discovered, threshold=0.6)
    assert upserts[0]["mode_id"] is None and retired == [7]
