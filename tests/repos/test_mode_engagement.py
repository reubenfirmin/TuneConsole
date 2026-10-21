"""#60 engagement requires explicit playlist provenance, account and selection time."""
import pytest


@pytest.fixture
def picked(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    other = store.identities.upsert_identity("other", "other-cred", None, False)
    pid = store.playlists.upsert_playlist(iid, "PL_saved", "Saved mode", 1, "h", 1)
    store.modes.log_pick(pid, 1, now=100)
    return iid, other, pid


@pytest.mark.parametrize("account,playlist,at,count", [
    ("main", "PL_saved", 100, 1),
    ("main", "PL_saved", 200, 1),
    ("main", "PL_saved", 99, 0),
    ("other", "PL_saved", 200, 0),
    ("main", "PL_other", 200, 0),
    ("main", None, 200, 0),
])
def test_attribution_applies_to_plays_and_exits(store, picked, account, playlist, at, count):
    iid, other, pid = picked
    identity = iid if account == "main" else other
    store.history.record_play_event(identity, "song|artist", "v", at, playlist_ytm_id=playlist)
    store.player_events.record_player_event(identity, "track_exit", "v", 190, 200, playlist, None, at)
    stats = store.modes.playlist_engagement()[pid]
    assert stats == {"plays": count, "last_play": at if count else None,
                     "completions": count, "skips": 0}


def test_exit_depth_uses_shared_classifier_without_multiplying_plays(store, picked):
    iid, _, pid = picked
    for i in range(3):
        store.history.record_play_event(iid, f"song {i}|artist", f"v{i}", 200 + i,
                                playlist_ytm_id="PL_saved")
    # Completion, skip, bounce, partial and unknown; state/tick observations are not exits.
    for i, (kind, position, duration) in enumerate([
        ("track_exit", 190, 200), ("bye", 20, 200), ("track_exit", 1, 200),
        ("track_exit", 100, 200), ("track_exit", None, None), ("tick", 190, 200),
    ]):
        store.player_events.record_player_event(iid, kind, "v", position, duration, "PL_saved", None, 300 + i)
    assert store.modes.playlist_engagement()[pid] == {
        "plays": 3, "last_play": 202, "completions": 1, "skips": 1}


def test_since_selects_the_same_pick_cohort_as_scoreboards(store, picked):
    iid, _, first = picked
    second = store.playlists.upsert_playlist(iid, "PL_later", "Later mode", 1, "h", 1)
    store.modes.log_pick(second, 2, now=500)
    for playlist in ("PL_saved", "PL_later"):
        store.history.record_play_event(iid, playlist + "|artist", None, 600, playlist_ytm_id=playlist)
    assert set(store.modes.playlist_engagement()) == {first, second}
    assert store.modes.playlist_engagement(since=200) == {
        second: {"plays": 1, "last_play": 600, "completions": 0, "skips": 0}}
