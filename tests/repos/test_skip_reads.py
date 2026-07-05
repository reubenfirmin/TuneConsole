"""#84 recent_skips_with_ts: read-time skip classification over player_events (track_exit/bye).

Classification is classify_exit(position, duration) from library.listen_derive (thresholds:
completion >= 85% listened, bounce < 3s listened, skip <= 30% AND <= 120s listened, else partial;
None/zero/missing duration -> unknown). Only rows that classify as "skip" are returned, deduped
per identity_key at the latest qualifying timestamp, newest-first.
"""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    return s


def _seed_track(s, video_id, title, artist="Artist"):
    """Registers a (video_id -> identity_key) mapping the way real sync does; returns the key."""
    s.upsert_track(video_id, title, artist, "Album", 400)
    return s.identity_key_for_video(video_id)


def test_skip_row_lands():
    s = _store()
    key = _seed_track(s, "v1", "Skip Song")
    # position=20, duration=400 -> ratio 0.05 <= 0.30 and position 20 <= 120s -> skip
    s.record_player_event(1, "track_exit", "v1", 20.0, 400.0, None, None, 1000.0)
    out = s.recent_skips_with_ts(0)
    assert out == [(key, 1000.0)]


def test_completion_row_excluded():
    s = _store()
    _seed_track(s, "v1", "Completion Song")
    # position=390, duration=400 -> ratio 0.975 >= 0.85 -> completion
    s.record_player_event(1, "track_exit", "v1", 390.0, 400.0, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == []


def test_partial_row_excluded():
    s = _store()
    _seed_track(s, "v1", "Partial Song")
    # position=200, duration=400 -> ratio 0.5 (> 0.30, position > 120s) -> partial
    s.record_player_event(1, "track_exit", "v1", 200.0, 400.0, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == []


def test_bounce_row_excluded():
    s = _store()
    _seed_track(s, "v1", "Bounce Song")
    # position=1.5 < 3.0s -> bounce (mis-click, not a taste signal)
    s.record_player_event(1, "track_exit", "v1", 1.5, 400.0, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == []


def test_bye_kind_skip_counts():
    s = _store()
    key = _seed_track(s, "v1", "Bye Skip Song")
    # bye is the other qualifying kind (tab-close mid-track); same classification rules apply
    s.record_player_event(1, "bye", "v1", 10.0, 400.0, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == [(key, 1000.0)]


def test_other_kinds_excluded():
    s = _store()
    _seed_track(s, "v1", "Tick Song")
    # a "tick" row with skip-shaped position/duration must not count: only track_exit/bye qualify
    s.record_player_event(1, "tick", "v1", 20.0, 400.0, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == []


def test_dedup_keeps_latest():
    s = _store()
    key = _seed_track(s, "v1", "Repeat Skip Song")
    s.record_player_event(1, "track_exit", "v1", 20.0, 400.0, None, None, 1000.0)
    s.record_player_event(1, "track_exit", "v1", 15.0, 400.0, None, None, 5000.0)
    out = s.recent_skips_with_ts(0)
    assert out == [(key, 5000.0)]


def test_since_filter():
    s = _store()
    key = _seed_track(s, "v1", "Old Skip Song")
    s.record_player_event(1, "track_exit", "v1", 20.0, 400.0, None, None, 1000.0)
    s.record_player_event(1, "track_exit", "v1", 20.0, 400.0, None, None, 9000.0)
    out = s.recent_skips_with_ts(5000.0)
    assert out == [(key, 9000.0)]


def test_null_duration_excluded():
    s = _store()
    _seed_track(s, "v1", "Null Duration Song")
    s.record_player_event(1, "track_exit", "v1", 20.0, None, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == []


def test_zero_duration_excluded():
    s = _store()
    _seed_track(s, "v1", "Zero Duration Song")
    s.record_player_event(1, "track_exit", "v1", 20.0, 0.0, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == []


def test_unknown_video_id_excluded():
    s = _store()
    # video_id never registered in tracks -> no identity_key -> must not surface even though the
    # position/duration shape is a skip
    s.record_player_event(1, "track_exit", "unregistered_v", 20.0, 400.0, None, None, 1000.0)
    assert s.recent_skips_with_ts(0) == []


def test_newest_first_across_distinct_keys():
    s = _store()
    key_a = _seed_track(s, "va", "Song A")
    key_b = _seed_track(s, "vb", "Song B")
    s.record_player_event(1, "track_exit", "va", 20.0, 400.0, None, None, 1000.0)
    s.record_player_event(1, "track_exit", "vb", 20.0, 400.0, None, None, 2000.0)
    out = s.recent_skips_with_ts(0)
    assert out == [(key_b, 2000.0), (key_a, 1000.0)]
