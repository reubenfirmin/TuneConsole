"""NOW classifies eligible plays in the current session and TODAY follows the listening calendar."""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tests.repository_mock_support import no_database, repository_store
from yt_playlist.library import listening_composition as layers
from yt_playlist.rec import embedding_vectors as embed


@pytest.fixture
def now_store(no_database):
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
        values={'transient_radio_playlists': set(), 'memoized_read': lambda _, read: read()},
    )


def recent_plays(store, keys, now):
    store.history.play_events_since.return_value = [
        {'identity_key': key, 'played_at': now - i * 60}
        for i, key in enumerate(keys)
    ]
    store.history.live_play_activity.side_effect = lambda start, end: [
        {'at': row['played_at']} for row in store.history.play_events_since.return_value
        if start <= row['played_at'] <= end]


def _identity(store):
    return store.accounts.upsert_account("main", None, True)


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
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: (keys, V, idx))
    return V, idx


def test_recent_plays_near_one_mode_yield_near_certain_posterior(now_store, monkeypatch):
    store = now_store
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    recent_plays(store, keys, now)

    posterior = layers.now_mode_posterior(store, now)
    assert posterior is not None
    assert posterior[1] == pytest.approx(1.0, abs=1e-6)
    assert 2 not in posterior
    store.history.play_events_since.assert_called_once_with(
        86400, exclude_list_ids=[], taste_only=True)
    assert set(store.rec_model.content_generation.call_args.kwargs['keys']) == set(keys)
    store.modes.list_modes.assert_called_once_with(active_only=True)


def test_mixed_plays_split_proportionally_by_count(now_store, monkeypatch):
    store = now_store
    keys = ["a1", "a2", "b1"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [0.02, 1.0]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    recent_plays(store, keys, now)

    posterior = layers.now_mode_posterior(store, now)
    assert posterior is not None
    assert posterior[1] == pytest.approx(2 / 3, abs=1e-6)
    assert posterior[2] == pytest.approx(1 / 3, abs=1e-6)


def test_below_min_events_returns_none(now_store, monkeypatch):
    store = now_store
    keys = ["a1", "a2"]
    V = np.array([[1.0, 0.02], [1.0, 0.03]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    recent_plays(store, keys, now)

    assert layers.now_mode_posterior(store, now) is None


def test_plays_older_than_window_are_excluded(store, monkeypatch):
    _install_modes(store)
    keys = ["a1", "a2", "a3", "a4", "a5"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01], [1.0, 0.04], [1.0, 0.05]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    iid = _identity(store)
    now = 100_000.0
    # Three plays in an earlier session and two in the current one.
    old_rows = [(keys[i], "v" + keys[i], now - 4000 - i * 60) for i in range(3)]
    new_rows = [(keys[i], "v" + keys[i], now - i * 60) for i in range(3, 5)]
    for key, video, at in sorted(old_rows + new_rows, key=lambda row: row[2]):
        store.history.record_play_event(iid, key, video, at, source_timezone='UTC')

    assert layers.now_mode_posterior(store, now) is None


def test_effective_mode_preferences_combine_today_recent_and_baseline(store, monkeypatch):
    from yt_playlist.rec import rec_params, scoring

    _install_modes(store)
    keys = ['h1', 'h2', 'h3']
    _install_content_vectors(store, monkeypatch, keys, np.array([[1., 0.]] * 3))
    account = _identity(store)
    now = 100_000.
    for index, key in enumerate(keys):
        store.history.record_play_event(account, key, 'v' + key, now - 4100 + index * 60, source_timezone='UTC')
    store.rec_model.set_weight('genre:techno', 1.4, now=now)
    facets = scoring.facet_preferences(store, now, recent={'genre:house': .4})
    modes = store.modes.list_modes(active_only=True)

    assert layers.now_mode_posterior(store, now) is None
    before_midnight = scoring.mode_preferences(store, modes, now, facets)
    after_midnight = scoring.mode_preferences(store, modes, 2 * 86400., facets)

    assert after_midnight[1] > 1.
    assert before_midnight[1] == pytest.approx(after_midnight[1] * (1 + rec_params.get_param(store, 'session_alpha')))
    assert before_midnight[2] == after_midnight[2] == pytest.approx(1.4)


def test_no_active_modes_returns_none(now_store, monkeypatch):
    store = now_store
    store.modes.list_modes.return_value = []
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    recent_plays(store, keys, now)

    assert layers.now_mode_posterior(store, now) is None


def test_no_content_vectors_returns_none(now_store, monkeypatch):
    store = now_store
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: ([], None, {}))
    now = 100_000.0
    keys = ["a1", "a2", "a3"]
    recent_plays(store, keys, now)

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
    for key, video, at in rows:
        store.history.record_play_event(iid, key, video, at)
    # a history-day noon bucket for the third key, timestamped inside the NOW window
    store.history.record_history_plays(iid, now, [keys[2]])

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
    from yt_playlist.core import radio_provenance
    with store.conn:
        radio_provenance.remember(store.conn, 'PLRADIO', iid)
    # Radio-queued plays (mode 1 / "X"): carry the radio playlist's own ytm id as provenance.
    for i, k in enumerate(["x1", "x2", "x3"]):
        store.history.record_play_event(iid, k, "v" + k, now - i * 60, playlist_ytm_id="PLRADIO")
    # Organic plays (mode 2 / "Y"): no provenance, e.g. a direct search/album play.
    for i, k in enumerate(["y1", "y2", "y3"]):
        store.history.record_play_event(iid, k, "v" + k, now - 200 - i * 60, playlist_ytm_id=None)

    posterior = layers.now_mode_posterior(store, now)
    assert posterior is not None
    assert posterior == {2: pytest.approx(1.0, abs=1e-6)}   # mode 1 (the radio-queued plays) absent


def test_stale_mode_centroids_dim_mismatch_reads_as_quiet(now_store, monkeypatch):
    # Live find (home render traceback): autotune can rebuild the content space at a NEW dim while
    # the stored mode centroids are still the old dim; the next mode rebuild heals them. That
    # window must read as quiet (None), never crash the render with a matmul shape error.
    store = now_store                                        # 2-D centroids
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.0, 0.0], [1.0, 0.1, 0.0], [1.0, 0.0, 0.1]], dtype=np.float32)   # 3-D
    _install_content_vectors(store, monkeypatch, keys, V)
    now = 100_000.0
    recent_plays(store, keys, now)

    shares, n, modes = layers.now_mode_mix(store, now)
    assert shares is None and n == 0 and len(modes) == 2


def test_playback_continuity_crosses_local_midnight_and_today_resets(store):
    zone = ZoneInfo('America/Costa_Rica')
    midnight = datetime(2026, 2, 20, tzinfo=zone).timestamp()
    account = _identity(store)
    store.history.record_play_event(account, 'evening', 'v1', midnight - 2400, source_timezone=zone.key)
    store.player_events.record_player_event(account, 'tick', 'v1', 1500, 2700, None, '{}', midnight - 1200)
    store.player_events.record_player_event(account, 'tick', 'v1', 2720, 2900, None, '{}', midnight + 20)
    store.history.record_play_event(account, 'morning', 'v2', midnight + 60, source_timezone=zone.key)
    store.history.import_listening_events(account, [('imported', 'vi', midnight + 70)], source_timezone=zone.key)

    assert layers.layer_plays(store, midnight - 10).today == {'evening': midnight - 2400}
    readings = layers.layer_plays(store, midnight + 120)
    assert readings.current == {'evening': midnight - 2400, 'morning': midnight + 60}
    assert readings.today == {'morning': midnight + 60, 'imported': midnight + 70}
    assert readings.earlier_today() == {'imported': midnight + 70}

    store.history.record_play_event(account, 'later', 'v3', midnight + 4000, source_timezone=zone.key)
    readings = layers.layer_plays(store, midnight + 4100)
    assert readings.current == {'later': midnight + 4000}
    assert set(readings.today) == {'morning', 'imported', 'later'}


def test_today_observes_short_day_at_daylight_saving_boundary():
    from yt_playlist.rec.layers import calendar_window

    zone = ZoneInfo('America/New_York')
    now = datetime(2026, 3, 8, 12, tzinfo=zone).timestamp()
    start, end = calendar_window(now, zone.key)
    assert datetime.fromtimestamp(start, zone).hour == datetime.fromtimestamp(end, zone).hour == 0
    assert end - start == 23 * 3600


@pytest.mark.parametrize('preplay', [False, True])
def test_taste_steering_expires_when_the_bound_session_ends(store, preplay):
    from yt_playlist.rec import rec_params, scoring

    account = _identity(store)
    store.history.record_play_event(account, 'old', 'vold', 1000., source_timezone='UTC')
    if preplay:
        rec_params.set_weight(store, 'genre:jazz', 1.7, now=900.)
    else:
        rec_params.set_weight(store, 'genre:jazz', 1.7, now=1100.)
    assert store.rec_model.get_weights(now=1100.) == {'genre:jazz': 1.7}
    assert scoring.discovery_facet_weight(store, 'jazz', 1100., leans={}) == 1.7
    assert store.rec_model.weight_records() == {}

    store.player_events.record_player_event(account, 'tick', 'vold', 1500, 2700, None, '{}', 2500.)
    assert store.rec_model.get_weights(now=3600.) == {'genre:jazz': 1.7}
    store.player_events.record_player_event(account, 'bye', 'vold', 2000, 2700, None, '{}', 3700.)
    store.history.record_play_event(account, 'new', 'vnew', 3730., source_timezone='UTC')
    assert store.rec_model.get_weights(now=3740.) == {}
    assert scoring.discovery_facet_weight(store, 'jazz', 3740., leans={}) == 1.
