"""Tests for #88 session_tilt layer."""
import numpy as np
from yt_playlist.rec import layers, rec_params
from yt_playlist.util.matching import identity_key


def test_hour_old_play_dominates_12h_old_at_hl4h(store):
    """At session_halflife=4h, an hour-old play should contribute more weight than a 12-hour-old one,
    so the tilt vector should be closer to the fresh play's direction. Weight ratio:
    decay_weight(3600, 4/24) / decay_weight(43200, 4/24) = 0.5**(1/4) / 0.5**(3) ≈ 6.73"""
    iid = store.upsert_identity("main", "test", None, True)

    # Create two distinct test vectors: one pointing along [1, 0] and another along [0, 1]
    # (will embed them in the full space by padding with zeros)
    v1_raw = np.array([1.0, 0.0], dtype=np.float64)
    v2_raw = np.array([0.0, 1.0], dtype=np.float64)

    # Create content vectors and indices
    V = np.zeros((2, 2), dtype=np.float64)
    V[0] = v1_raw
    V[1] = v2_raw
    idx = {"track1|artist1": 0, "track2|artist2": 1}

    now = 1000000.0
    # Record two plays at different ages
    store.import_play_events(iid, [
        ("track1|artist1", "v1", now - 3600.0),    # 1 hour old
        ("track2|artist2", "v2", now - 43200.0),   # 12 hours old
    ])

    # Set session_halflife to 4 hours
    rec_params.set_param(store, "session_halflife_h", 4.0)
    rec_params.set_param(store, "now_min_events", 2)

    tilt = layers.session_tilt(store, now, V, idx)
    assert tilt is not None, "tilt should not be None with 2 contributing plays"

    # The tilt should be closer to v1 (1-hour-old) than v2 (12-hour-old)
    # Cosine similarity with v1: closer to 1.0 means more aligned
    cos_v1 = np.dot(tilt, v1_raw / np.linalg.norm(v1_raw))
    cos_v2 = np.dot(tilt, v2_raw / np.linalg.norm(v2_raw))

    assert cos_v1 > cos_v2, f"Tilt should be closer to fresh play (cos_v1={cos_v1:.4f}) than stale (cos_v2={cos_v2:.4f})"

    # Verify approximate weight ratio
    from yt_playlist.rec.transient import decay_weight
    w1 = decay_weight(3600.0, 4.0 / 24.0)
    w12 = decay_weight(43200.0, 4.0 / 24.0)
    ratio = w1 / w12
    expected_ratio = 0.5 ** (1.0 / 4.0) / 0.5 ** (12.0 / 4.0)
    assert abs(ratio - expected_ratio) < 0.001, f"Weight ratio {ratio} should match expected {expected_ratio}"


def test_quiet_store_returns_none(store):
    """When there are no play events in the last 24 hours, session_tilt returns None."""
    V = np.random.randn(10, 5).astype(np.float64)
    idx = {f"track{i}|artist{i}": i for i in range(10)}

    now = 1000000.0
    # No plays recorded
    rec_params.set_param(store, "now_min_events", 2)

    tilt = layers.session_tilt(store, now, V, idx)
    assert tilt is None, "tilt should be None when no plays in the window"


def test_below_gate_returns_none(store):
    """When fewer than now_min_events keys contribute, session_tilt returns None."""
    iid = store.upsert_identity("main", "test", None, True)

    V = np.random.randn(5, 3).astype(np.float64)
    idx = {f"track{i}|artist{i}": i for i in range(5)}

    now = 1000000.0
    # Record only 1 play
    store.import_play_events(iid, [
        ("track0|artist0", "v0", now - 3600.0),
    ])

    # Gate requires 3 contributing plays
    rec_params.set_param(store, "now_min_events", 3)

    tilt = layers.session_tilt(store, now, V, idx)
    assert tilt is None, "tilt should be None when fewer than now_min_events contributing keys"


def test_keys_absent_from_idx_do_not_contribute(store):
    """Keys in play_events that are absent from idx should not contribute to tilt or count toward gate."""
    iid = store.upsert_identity("main", "test", None, True)

    V = np.eye(3, dtype=np.float64)
    idx = {"track0|artist0": 0, "track1|artist1": 1}  # Only 2 keys in idx

    now = 1000000.0
    # Record 3 plays: 2 in idx, 1 absent
    store.import_play_events(iid, [
        ("track0|artist0", "v0", now - 3600.0),
        ("track1|artist1", "v1", now - 7200.0),
        ("track2|artist2", "v2", now - 10800.0),  # This key is NOT in idx
    ])

    # Gate requires 2 contributing plays (the 3rd play should not count)
    rec_params.set_param(store, "now_min_events", 2)
    rec_params.set_param(store, "session_halflife_h", 4.0)

    tilt = layers.session_tilt(store, now, V, idx)
    # With gate=2 and only 2 contributing keys, this should succeed
    assert tilt is not None, "tilt should not be None with 2 contributing keys (absent key should not count)"

    # Now test that the absent key doesn't contribute: if we raise the gate to 3,
    # it should fail because only 2 keys are in idx
    rec_params.set_param(store, "now_min_events", 3)
    tilt = layers.session_tilt(store, now, V, idx)
    assert tilt is None, "tilt should be None when gate=3 but only 2 keys are in idx"


def test_zero_norm_contribution_returns_none(store):
    """When a key's vector has zero norm, it should be skipped. If all vectors are zero-norm,
    the result should be None."""
    iid = store.upsert_identity("main", "test", None, True)

    # Create vectors with zero norm
    V = np.zeros((3, 4), dtype=np.float64)
    idx = {f"track{i}|artist{i}": i for i in range(3)}

    now = 1000000.0
    store.import_play_events(iid, [
        ("track0|artist0", "v0", now - 3600.0),
        ("track1|artist1", "v1", now - 7200.0),
        ("track2|artist2", "v2", now - 10800.0),
    ])

    rec_params.set_param(store, "now_min_events", 2)

    tilt = layers.session_tilt(store, now, V, idx)
    # All vectors are zero-norm, so accumulation is zero -> None
    assert tilt is None, "tilt should be None when all contributing vectors are zero-norm"
