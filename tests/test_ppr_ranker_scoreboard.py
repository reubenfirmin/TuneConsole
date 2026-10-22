import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import mode_eval
from tests.mode_fixtures import seed_modes


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    seed_modes(s,1,2)
    return s


def test_ranker_scoreboard_aggregates_ab(store):
    # ppr offered 2, cosine offered 1
    store.modes.log_impressions(1, [("wheelhouse", 1, "ppr"), ("explore", 2, "ppr")], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1, "cosine")], now=110.0)
    iid = store.identities.upsert_identity("main", "cred", None, True)
    for i, (ranker, plays) in enumerate((("ppr", 5), ("ppr", 3), ("cosine", 1))):
        ytm = f"PL_saved_{i}"
        pid = store.playlists.upsert_playlist(iid, ytm, ranker, plays, "h", 1)
        store.modes.log_pick(pid, 1, 120 + i, ranker=ranker)
        for j in range(plays):
            store.history.record_play_event(iid, f"song {i} {j}|artist", None, 200 + j,
                                    playlist_ytm_id=ytm)
        store.player_events.record_player_event(iid, "track_exit", "v", 190, 200, ytm, None, 300)
        # An organic play of any playlist's song cannot increase a ranker's engagement.
        store.history.record_play_event(iid, f"song {i} 0|artist", None, 400)

    board = {r["ranker"]: r for r in mode_eval.ranker_scoreboard(store)}
    assert board["ppr"] == {"ranker": "ppr", "offered": 2, "picked": 2, "plays": 8,
                            "skips": 0, "completions": 2}
    assert board["cosine"] == {"ranker": "cosine", "offered": 1, "picked": 1, "plays": 1,
                               "skips": 0, "completions": 1}


def test_ranker_scoreboard_empty(store):
    assert mode_eval.ranker_scoreboard(store) == []
