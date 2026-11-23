"""TODAY mode shares apply time decay within the listening calendar day."""
from unittest.mock import Mock

import numpy as np
import pytest

from tests.repository_mock_support import no_database, repository_store
from yt_playlist.library import listening_composition as layers
from yt_playlist.rec import embedding_vectors as embed, rec_params


@pytest.fixture
def session_store(no_database):
    return repository_store(
        settings={'get_setting': None},
        rec_model={'content_generation': {'model': None, 'library': []}},
        history={'play_events_since': [], 'listening_calendars': {None: 'UTC'}, 'live_play_activity': []},
        player_events={'playback_activity': []},
        rec_eligibility={'generated_only_keys': set()},
        playlists={'radio_playlist_ids': []},
        genres={'feature_axes': {}},
        modes={'list_modes': [
            {'mode_id': 1, 'centroid': np.array([1., 0.], dtype=np.float32)},
            {'mode_id': 2, 'centroid': np.array([0., 1.], dtype=np.float32)},
        ]},
        values={'transient_radio_playlists': set(),
                'memoized_read': Mock(side_effect=lambda key, calculate: calculate())},
    )


def _parameters(store, **values):
    store.settings.get_setting.side_effect = {
        rec_params.SETTING_PREFIX + name: str(value) for name, value in values.items()
    }.get


def _recent_plays(store, keys, now):
    store.history.play_events_since.return_value = [
        {'identity_key': key, 'played_at': now - i * 60}
        for i, key in enumerate(keys)
    ]


def _identity(store):
    return store.accounts.upsert_account("main", None, True)


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
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: (keys, V, idx))
    return V, idx


def test_decay_weighted_shares_hand_verified(session_store, monkeypatch):
    store = session_store
    # Hand-verified arithmetic: play "a1" (mode 1) at age 0h, play "b1" (mode 2) at age 12h, with a
    # 12h session half-life.
    #   decay_weight(age=0s, half_life_d=12/24=0.5)  -> age_s <= 0 branch -> weight = 1.0
    #   decay_weight(age=12h=43200s, half_life_d=0.5) -> 0.5 ** (43200 / (0.5*86400))
    #                                                  = 0.5 ** (43200/43200) = 0.5 ** 1 = 0.5
    # Weights: mode1 = 1.0, mode2 = 0.5, total = 1.5
    # Shares:  mode1 = 1.0/1.5 = 2/3,   mode2 = 0.5/1.5 = 1/3
    keys = ["a1", "b1"]
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 150_000.0
    store.history.play_events_since.return_value = [
        {'identity_key': 'a1', 'played_at': now},
        {'identity_key': 'b1', 'played_at': now - 12 * 3600.0},
    ]
    _parameters(store, now_min_events=2, session_halflife_h=12.)

    evidence = layers.recent_evidence(store, now)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("Caller-supplied evidence must be reused")

    monkeypatch.setattr(layers, 'recent_evidence', unexpected_read)
    shares, n, modes = layers.session_mode_mix(store, now, evidence=evidence)
    assert shares is not None
    assert n == 2
    assert shares[1] == pytest.approx(2 / 3, abs=1e-6)
    assert shares[2] == pytest.approx(1 / 3, abs=1e-6)
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-9)
    assert {m["mode_id"] for m in modes} == {1, 2}
    assert modes is evidence.modes
    flat, count, active = layers.now_mode_mix(store, now, evidence=evidence)
    assert flat is None and count == 0 and active is evidence.modes
    store.history.play_events_since.assert_called_once_with(
        86400, exclude_list_ids=[], taste_only=True)


def test_below_min_events_returns_none(session_store, monkeypatch):
    store = session_store
    keys = ["a1", "a2"]
    V = np.array([[1.0, 0.02], [1.0, 0.03]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    _recent_plays(store, keys, now)
    _parameters(store, now_min_events=3)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None
    assert n == 0


def test_yesterday_excluded_even_within_24h(store, monkeypatch):
    # The prior day's play would clear the confidence gate if TODAY were rolling.
    _install_modes(store)
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    for key, video, at in [
        ("a1", "va1", now - 3600.0),
        ("a2", "va2", now - 7200.0),
        ("a3", "va3", now - 6 * 3600.0),
    ]:
        store.history.record_play_event(iid, key, video, at, source_timezone='UTC')
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
    for key, video, at in [
        ("a1", "va1", now - 2 * 3600.0),  # a1 played earlier today...
        ("a1", "va1", now),                # ...and again just now (this timestamp should win)
        ("b1", "vb1", now - 3600.0),
    ]:
        store.history.record_play_event(iid, key, video, at, source_timezone='UTC')
    rec_params.set_param(store, "now_min_events", 2)
    rec_params.set_param(store, "session_halflife_h", 4.0)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is not None
    assert n == 2   # a1 counted once, not twice
    # a1's weight uses age=0 (the latest play), giving it near-full weight vs b1's 1h-old play.
    assert shares[1] > shares[2]


def test_no_active_modes_returns_none(session_store, monkeypatch):
    store = session_store
    store.modes.list_modes.return_value = []
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    _recent_plays(store, keys, now)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None
    assert n == 0
    assert modes == []


def test_no_content_vectors_returns_none(session_store, monkeypatch):
    store = session_store
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: ([], None, {}))
    now = 100_000.0
    keys = ["a1", "a2", "a3"]
    _recent_plays(store, keys, now)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None
    assert n == 0


def test_session_mode_mix_passes_radio_exclusion_to_play_events_since(session_store, monkeypatch):
    """#93v2 pin: session_mode_mix must resolve the radio playlist ids (layers._radio_list_ids) and
    pass them as exclude_list_ids to store.history.play_events_since. Behavior is covered end-to-end by
    test_now_layer.py's equivalent test for the NOW layer (same underlying mechanism); this pins the
    parameter-passing contract for THIS call site specifically."""
    store = session_store
    _install_content_vectors(store, monkeypatch, ["a1"], np.array([[1.0, 0.0]], dtype=np.float32))
    store.playlists.radio_playlist_ids.return_value = ['PLRADIO', 'PLRADIO_B']

    layers.session_mode_mix(store, 1000.0)

    store.history.play_events_since.assert_called_once_with(
        0., exclude_list_ids=['PLRADIO', 'PLRADIO_B'], taste_only=True)
    store.playlists.radio_playlist_ids.assert_called_once_with()


def test_stale_mode_centroids_dim_mismatch_reads_as_quiet(session_store, monkeypatch):
    store = session_store
    # Same stale-modes window as the NOW layer (content space rebuilt at a new dim before the mode
    # rebuild catches up): quiet, never a shape-error crash.
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.0, 0.0], [1.0, 0.1, 0.0], [1.0, 0.0, 0.1]], dtype=np.float32)   # 3-D
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    _recent_plays(store, keys, now)

    shares, n, modes = layers.session_mode_mix(store, now)
    assert shares is None and n == 0 and len(modes) == 2
