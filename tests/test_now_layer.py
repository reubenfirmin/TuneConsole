"""#88 NOW layer: a confidence-gated posterior over taste modes from the last few hours of real
plays. Quiet hours and thin evidence return None, never a weak guess."""
import numpy as np
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import embed, layers


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _identity(store):
    return store.upsert_identity("main", "cred", None, True)


def _install_modes(store):
    """Two orthogonal active taste modes in a 2-D content space."""
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


def test_recent_plays_near_one_mode_yield_near_certain_posterior(store, monkeypatch):
    _install_modes(store)
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.import_play_events(iid, rows)

    posterior = layers.now_mode_posterior(store, now)
    assert posterior is not None
    assert posterior[1] == pytest.approx(1.0, abs=1e-6)
    assert 2 not in posterior


def test_mixed_plays_split_proportionally_by_count(store, monkeypatch):
    _install_modes(store)
    keys = ["a1", "a2", "b1"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [0.02, 1.0]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.import_play_events(iid, rows)

    posterior = layers.now_mode_posterior(store, now)
    assert posterior is not None
    assert posterior[1] == pytest.approx(2 / 3, abs=1e-6)
    assert posterior[2] == pytest.approx(1 / 3, abs=1e-6)


def test_below_min_events_returns_none(store, monkeypatch):
    _install_modes(store)
    keys = ["a1", "a2"]
    V = np.array([[1.0, 0.02], [1.0, 0.03]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.import_play_events(iid, rows)

    assert layers.now_mode_posterior(store, now) is None


def test_plays_older_than_window_are_excluded(store, monkeypatch):
    _install_modes(store)
    keys = ["a1", "a2", "a3", "a4", "a5"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01], [1.0, 0.04], [1.0, 0.05]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    window_s = 6 * 3600
    # 3 plays well outside the 6h window, 2 plays inside it -> only 2 count, below the gate of 3
    old_rows = [(keys[i], "v" + keys[i], now - window_s - 3600 - i * 60) for i in range(3)]
    new_rows = [(keys[i], "v" + keys[i], now - i * 60) for i in range(3, 5)]
    store.import_play_events(iid, old_rows + new_rows)

    assert layers.now_mode_posterior(store, now) is None


def test_no_active_modes_returns_none(store, monkeypatch):
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.import_play_events(iid, rows)

    assert layers.now_mode_posterior(store, now) is None


def test_no_content_vectors_returns_none(store, monkeypatch):
    _install_modes(store)
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: ([], None, {}))
    iid = _identity(store)
    now = 100_000.0
    keys = ["a1", "a2", "a3"]
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.import_play_events(iid, rows)

    assert layers.now_mode_posterior(store, now) is None


def test_history_day_noon_bucket_does_not_count_toward_now_layer(store, monkeypatch):
    """play_events_since reads play_events only; a history-day (noon-bucket) row landing inside the
    window must not feed the NOW layer, even though it is within the same wall-clock hours."""
    _install_modes(store)
    keys = ["a1", "a2", "h1"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    # only 2 real play_events rows -> below now_min_events=3 on their own
    rows = [(keys[i], "v" + keys[i], now - i * 60) for i in range(2)]
    store.import_play_events(iid, rows)
    # a history-day noon bucket for the third key, timestamped inside the NOW window
    store.record_history_plays(iid, now, [keys[2]])

    assert layers.now_mode_posterior(store, now) is None


def test_radio_provenance_plays_are_excluded_from_now_layer_mood_evidence(store, monkeypatch):
    """#93v2 loop fix: the radio's own machine-queued plays must not feed the NOW layer, or a radio
    pick tilts the very posterior that steers the next radio pick. Seed radio-provenance plays for
    mode 1 (would-be evidence for "X") and organic plays for mode 2 ("Y"); the posterior must read
    as pure Y, never tilted toward X by the plays radio itself queued."""
    _install_modes(store)
    keys = ["x1", "x2", "x3", "y1", "y2", "y3"]
    V = np.array([
        [1.0, 0.02], [1.0, 0.03], [1.0, 0.01],   # x1..x3 -> mode 1
        [0.02, 1.0], [0.03, 1.0], [0.01, 1.0],   # y1..y3 -> mode 2
    ], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    store.set_setting("radio_playlist_ytm", "PLRADIO")
    # Radio-queued plays (mode 1 / "X"): carry the radio playlist's own ytm id as provenance.
    for i, k in enumerate(["x1", "x2", "x3"]):
        store.record_play_event(iid, k, "v" + k, now - i * 60, playlist_ytm_id="PLRADIO")
    # Organic plays (mode 2 / "Y"): no provenance, e.g. a direct search/album play.
    for i, k in enumerate(["y1", "y2", "y3"]):
        store.record_play_event(iid, k, "v" + k, now - 200 - i * 60, playlist_ytm_id=None)

    posterior = layers.now_mode_posterior(store, now)
    assert posterior is not None
    assert posterior == {2: pytest.approx(1.0, abs=1e-6)}   # mode 1 (the radio-queued plays) absent


def test_stale_mode_centroids_dim_mismatch_reads_as_quiet(store, monkeypatch):
    # Live find (home render traceback): autotune can rebuild the content space at a NEW dim while
    # the stored mode centroids are still the old dim; the next mode rebuild heals them. That
    # window must read as quiet (None), never crash the render with a matmul shape error.
    _install_modes(store)                                     # 2-D centroids
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.0, 0.0], [1.0, 0.1, 0.0], [1.0, 0.0, 0.1]], dtype=np.float32)   # 3-D
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    store.import_play_events(iid, [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)])

    assert layers.now_mode_posterior(store, now) is None
