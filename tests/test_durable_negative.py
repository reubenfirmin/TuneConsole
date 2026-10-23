"""#84: explicit dislikes remain bounded vector evidence after transient signals fade."""
import numpy as np
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import graduation, mode_surfaces, negative, rec_params, scoring, transient


def test_dislike_direction_survives_restart_pruning_and_ban_expiry(tmp_path):
    path = tmp_path / "taste.db"
    store = Store(path)
    store.init_schema()
    tid = store.tracks.upsert_track("bad", "Bad", "Artist", None, None)
    store.tracks.set_track_genre(tid, "jazz")
    key = store.tracks.identity_key_for_video("bad")
    graduation.apply_dislikes(store, {key: "DISLIKE"}, now=100)
    store.rec_surface.record_mood([key], -1, now=100)
    V = np.array([[1., 0.], [0.9, 0.1], [0., 1.], [-1., 0.]])
    idx = {key: 0, "similar": 1, "unrelated": 2, "opposite": 3}
    fresh = scoring._mood_components(np.zeros(4), store, 100, V, idx)
    store.conn.close()

    store = Store(path)
    store.init_schema()
    later = 100 + 2000 * 86400
    store.rec_surface.record_mood([], 1, now=later, prune_before=later - 1)
    assert key not in store.rec_model.suppressed_keys("for_you", later)
    assert abs(transient.facet_leans(store, later)["genre:jazz"]) < 1e-10
    aged = scoring._mood_components(np.zeros(4), store, later, V, idx)
    assert aged["negative"] == pytest.approx(fresh["negative"])
    assert aged["negative"][1] < 0
    assert aged["negative"][2] == aged["negative"][3] == 0
    assert aged["combined"] == pytest.approx(aged["negative"])

    graduation.apply_dislikes(store, {key: "INDIFFERENT"}, later)
    assert negative.penalties(store, V, idx) == pytest.approx(np.zeros(4))
    store.conn.close()


def test_negative_vectors_are_bounded_missing_and_zero_are_neutral(store):
    V = np.array([[10., 0.], [0., 0.], [0., 3.], [-10., 0.]])
    idx = {"bad": 0, "zero": 1, "other": 2, "opposite": 3}
    for key in ("bad", "zero", "missing"):
        store.rec_model.record_dislike(key, 1000, 100)
    deltas = negative.penalties(store, V, idx)
    weight = rec_params.get_param(store, "dislike_durable_w")
    assert deltas == pytest.approx([-weight, 0, 0, 0])
    store.rec_model.record_dislike("opposite", 1000, 100)
    assert negative.penalties(store, V, idx) == pytest.approx(np.zeros(4))
    rec_params.set_param(store, "dislike_durable_w", 0)
    assert negative.penalties(store, V, idx) == pytest.approx(np.zeros(4))


def test_cold_candidates_use_dislike_evidence_outside_the_candidate_pool(store):
    store.rec_model.record_dislike("warm-dislike", 1000, 100)
    evidence = np.array([[1., 0.], [0., 1.]])
    V = np.array([[0.9, 0.1], [0., 1.]])
    components = scoring._mood_components([0.5, 0.5], store, 100, V,
        {"cold-similar": 0, "cold-other": 1},
        evidence_vecs=(evidence, {"warm-dislike": 0, "warm-other": 1}))
    assert components["combined"][0] < components["combined"][1]


@pytest.mark.parametrize("ranker", ["cosine", "ppr"])
def test_prepared_card_order_applies_durable_dislike_delta(ranker):
    items = [{"key": "similar"}, {"key": "unrelated"}]
    order = mode_surfaces._order_key(ranker, {"similar": 0, "unrelated": 1,
        "third": 2, "fourth": 3}, {}, {"similar": -0.35})
    assert [item["key"] for item in sorted(items, key=order)] == ["unrelated", "similar"]
