"""Ranking contributions retain wall-clock decay in every vector space (#85, #45, #88)."""
import numpy as np
import pytest

from yt_playlist.rec import rec_params, scoring, transient

DAY = 86400.0


@pytest.mark.parametrize("space", ["collaborative", "acoustic"])
@pytest.mark.parametrize("source", ["mood", "play", "like", "skip"])
def test_ranking_contribution_fades_by_source_half_life(store, space, source):
    now = 1000 * DAY
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v", "Song", "Artist", None, 200)
    key = "song|artist"
    keys, vectors = [key, "other|artist"], np.eye(2, dtype=np.float32)
    store.replace_rec_content_vectors([(k, v.tobytes()) for k, v in zip(keys, vectors)])
    rec_params.set_param(store, "session_alpha", 0)
    rec_params.set_param(store, "audio_transient_w", 0.3 if space == "acoustic" else 0)
    if source == "mood":
        store.record_mood([key], 1, now=now)
    elif source == "play":
        store.record_play_event(iid, key, "v", now)
    elif source == "like":
        store.record_like(key, now, provenance="action")
    else:
        store.record_player_event(iid, "track_exit", "v", 20, 200, None, None, now)

    # With acoustic scoring, zero collaborative rows isolate the real content-space producer.
    V = np.zeros_like(vectors) if space == "acoustic" else vectors
    idx = {k: i for i, k in enumerate(keys)}
    component = "audio" if space == "acoustic" else "transient"
    sign = -1 if source == "skip" else 1
    base = np.array([0.1, 0]) if source == "skip" else np.array([0, 0.1])
    half_life = rec_params.get_param(store, source + "_halflife_d")

    def components(age):
        return scoring._mood_components(base, store, now + age * DAY, V, idx)

    fresh, half, old = (components(age) for age in (0, half_life, 3 * half_life))
    assert sign * fresh[component][0] > 0
    np.testing.assert_allclose(half[component], fresh[component] / 2)
    np.testing.assert_allclose(old[component], fresh[component] / 8)
    # With no new evidence, durable taste regains the lead instead of staying overridden forever.
    assert fresh["combined"].argmax() == (0 if sign > 0 else 1)
    assert old["combined"].argmax() == (1 if sign > 0 else 0)


def test_missing_and_zero_vectors_do_not_dilute_bounded_tilt(store):
    now = 1000 * DAY
    V = np.array([[1, 0], [0, 1], [0, 0]], dtype=np.float32)
    idx = {"a|x": 0, "b|y": 1, "zero|z": 2}
    for key in ("a|x", "b|y", "missing|m", "zero|z"):
        store.record_mood([key], 1, now=now)
    fresh = transient.centroid_tilt(store, now, V, idx)
    np.testing.assert_allclose(fresh, [0.5, 0.5])
    assert np.linalg.norm(fresh) < 1  # disagreeing directions have less confidence
    half_life = rec_params.get_param(store, "mood_halflife_d")
    np.testing.assert_allclose(transient.centroid_tilt(store, now + half_life * DAY, V, idx),
                               fresh / 2)


def test_disabled_play_source_cannot_dilute_mood_signal(store):
    iid = store.upsert_identity("main", "cred", None, True)
    rec_params.set_param(store, "play_transient_w", 0)
    store.record_mood(["a|x"], 1, now=1000)
    store.record_play_event(iid, "b|y", "v", 1000)
    np.testing.assert_allclose(transient.centroid_tilt(
        store, 1000, np.eye(2), {"a|x": 0, "b|y": 1}), [1, 0])


def test_session_ranking_contribution_fades_without_new_plays(store):
    now = 1000 * DAY
    iid = store.upsert_identity("main", "cred", None, True)
    keys = ["a|x", "b|y", "other|z"]
    V = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
    idx = {k: i for i, k in enumerate(keys)}
    for key in keys[:2]:
        store.record_play_event(iid, key, None, now)
    rec_params.set_param(store, "now_min_events", 2)
    rec_params.set_param(store, "session_halflife_h", 4)
    rec_params.set_param(store, "audio_transient_w", 0)
    base = np.zeros(3)
    fresh = scoring._mood_components(base, store, now, V, idx)["session"]
    assert fresh[0] > 0
    for hours, divisor in ((4, 2), (12, 8)):
        aged = scoring._mood_components(base, store, now + hours * 3600, V, idx)["session"]
        np.testing.assert_allclose(aged, fresh / divisor)
    assert not scoring._mood_components(base, store, now + 25 * 3600, V, idx)["session"].any()
