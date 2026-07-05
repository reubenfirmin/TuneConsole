"""#88 SESSION layer's legible companion: `session_mode_mix` mirrors `now_mode_mix`'s nearest-mode
classification and confidence gate, but over a fixed 24h window with each played key's classification
decay-weighted by `session_halflife_h` instead of counted flat. See layers.session_mode_mix's
docstring for why this categorical mix exists alongside `session_tilt` (a direction that can't be
honestly labeled)."""
import numpy as np
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import embed, layers, rec_params


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _identity(store):
    return store.upsert_identity("main", "cred", None, True)


def _install_modes(store):
    """Two orthogonal active taste modes in a 2-D content space (mirrors test_now_layer.py)."""
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
        {"mode_id": 2, "label": "b", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)


def _install_content_vectors(store, monkeypatch, keys, V):
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    idx = {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    return V, idx


def test_decay_weighted_shares_hand_verified(store, monkeypatch):
    # Hand-verified arithmetic: play "a1" (mode 1) at age 0h, play "b1" (mode 2) at age 12h, with a
    # 12h session half-life.
    #   decay_weight(age=0s, half_life_d=12/24=0.5)  -> age_s <= 0 branch -> weight = 1.0
    #   decay_weight(age=12h=43200s, half_life_d=0.5) -> 0.5 ** (43200 / (0.5*86400))
    #                                                  = 0.5 ** (43200/43200) = 0.5 ** 1 = 0.5
    # Weights: mode1 = 1.0, mode2 = 0.5, total = 1.5
    # Shares:  mode1 = 1.0/1.5 = 2/3,   mode2 = 0.5/1.5 = 1/3
    _install_modes(store)
    keys = ["a1", "b1"]
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    store.import_play_events(iid, [
        ("a1", "va1", now),               # age 0h -> mode 1
        ("b1", "vb1", now - 12 * 3600.0),  # age 12h -> mode 2
    ])
    rec_params.set_param(store, "now_min_events", 2)
    rec_params.set_param(store, "session_halflife_h", 12.0)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is not None
    assert n == 2
    assert shares[1] == pytest.approx(2 / 3, abs=1e-6)
    assert shares[2] == pytest.approx(1 / 3, abs=1e-6)
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-9)
    assert {m["mode_id"] for m in modes} == {1, 2}


def test_below_min_events_returns_none(store, monkeypatch):
    _install_modes(store)
    keys = ["a1", "a2"]
    V = np.array([[1.0, 0.02], [1.0, 0.03]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    store.import_play_events(iid, [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)])
    rec_params.set_param(store, "now_min_events", 3)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None
    assert n == 0


def test_30h_old_play_excluded_by_24h_window(store, monkeypatch):
    # 2 fresh plays (inside the 24h window) + 1 play 30h old (outside it). Gate is 3, so if the old
    # play counted we'd clear the gate; since it's excluded, only 2 contribute and the read stays None.
    _install_modes(store)
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    store.import_play_events(iid, [
        ("a1", "va1", now - 3600.0),
        ("a2", "va2", now - 7200.0),
        ("a3", "va3", now - 30 * 3600.0),  # 30h old -> outside the fixed 24h session window
    ])
    rec_params.set_param(store, "now_min_events", 3)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None
    assert n == 0


def test_dedup_latest_per_key(store, monkeypatch):
    # Same key played twice inside the window: dedup keeps only the latest timestamp, so it counts
    # once toward n and its weight uses the latest (freshest) age, not the earlier one.
    _install_modes(store)
    keys = ["a1", "b1"]
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    # rows must arrive oldest-first for the dedup-latest-wins convention (mirrors test_now_layer.py).
    store.import_play_events(iid, [
        ("a1", "va1", now - 20 * 3600.0),  # a1 played long ago...
        ("a1", "va1", now),                # ...and again just now (this timestamp should win)
        ("b1", "vb1", now - 3600.0),
    ])
    rec_params.set_param(store, "now_min_events", 2)
    rec_params.set_param(store, "session_halflife_h", 4.0)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is not None
    assert n == 2   # a1 counted once, not twice
    # a1's weight uses age=0 (the latest play), giving it near-full weight vs b1's 1h-old play.
    assert shares[1] > shares[2]


def test_no_active_modes_returns_none(store, monkeypatch):
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    store.import_play_events(iid, [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)])

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None
    assert n == 0
    assert modes == []


def test_no_content_vectors_returns_none(store, monkeypatch):
    _install_modes(store)
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: ([], None, {}))
    iid = _identity(store)
    now = 100_000.0
    keys = ["a1", "a2", "a3"]
    store.import_play_events(iid, [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)])

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None
    assert n == 0


def test_session_mode_mix_passes_radio_exclusion_to_play_events_since(store, monkeypatch):
    """#93v2 pin: session_mode_mix must resolve the radio playlist ids (layers._radio_list_ids) and
    pass them as exclude_list_ids to store.play_events_since. Behavior is covered end-to-end by
    test_now_layer.py's equivalent test for the NOW layer (same underlying mechanism); this pins the
    parameter-passing contract for THIS call site specifically."""
    _install_modes(store)
    _install_content_vectors(store, monkeypatch, ["a1"], np.array([[1.0, 0.0]], dtype=np.float32))
    store.set_setting("radio_playlist_ytm", "PLRADIO")
    store.set_setting("radio_playlist_b_ytm", "PLRADIO_B")
    captured = {}
    orig = store.play_events_since

    def fake(since_ts, exclude_list_ids=None):
        captured["exclude_list_ids"] = exclude_list_ids
        return orig(since_ts, exclude_list_ids=exclude_list_ids)
    monkeypatch.setattr(store, "play_events_since", fake)

    layers.session_mode_mix(store, 1000.0)

    assert captured["exclude_list_ids"] == ["PLRADIO", "PLRADIO_B"]


def test_stale_mode_centroids_dim_mismatch_reads_as_quiet(store, monkeypatch):
    # Same stale-modes window as the NOW layer (content space rebuilt at a new dim before the mode
    # rebuild catches up): quiet, never a shape-error crash.
    _install_modes(store)                                     # 2-D centroids
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.0, 0.0], [1.0, 0.1, 0.0], [1.0, 0.0, 0.1]], dtype=np.float32)   # 3-D
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    store.import_play_events(iid, [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)])

    shares, _n, modes = layers.session_mode_mix(store, now)
    assert shares is None and len(modes) == 2
