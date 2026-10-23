"""Named recommendation repositories share one connection and lock, with distinct owners."""
from yt_playlist.repos.base import Repo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.rec_surface import RecSurfaceRepo


def test_named_recommendation_repositories(store):
    import pytest
    assert isinstance(store.rec_model, RecModelRepo)
    assert isinstance(store.rec_surface, RecSurfaceRepo)
    assert isinstance(store.rec_query, RecQueryRepo)
    store.rec_model.set_weight("lane:neighbourhood", 1.1)
    assert store.rec_model.get_weights()["lane:neighbourhood"] == pytest.approx(1.1)
    assert store.rec_query.tracks_total() == 0
    assert store.rec_surface.get_proposals("discover") is None


def test_unified_onto_repo_base(store):
    for dao in (store.rec_model, store.rec_surface, store.rec_query):
        assert isinstance(dao, Repo)
        assert dao.conn is store.conn and dao._lock is store._lock


def test_card_view_counter_ticks_and_reads(store):
    dao = store.rec_surface
    assert dao.card_views("wheelhouse") == 0                      # never shown
    assert dao.bump_card_view("wheelhouse", now=1000.0) == 1      # first tick
    assert dao.bump_card_view("wheelhouse", now=1100.0) == 2
    assert dao.card_views("wheelhouse") == 2                      # read-only, doesn't advance
    assert dao.card_views("wheelhouse") == 2
    assert dao.bump_card_view("comfort", now=1000.0) == 1         # independent per card
    assert dao.card_views("wheelhouse") == 2


def test_proposals_roundtrip_and_missing(store):
    dao = store.rec_surface
    assert dao.get_proposals("discover") is None
    dao.put_proposals("discover", [{"artist": "X", "title": "LP"}], now=1.0)
    assert dao.get_proposals("discover") == [{"artist": "X", "title": "LP"}]


def test_tracks_total_replaces_raw_sql(store):
    dao = store.rec_query
    assert dao.tracks_total() == store.conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]


# --- the learned-model methods folded in from the former god class ---

def test_weights_nudge_set_reset(store):
    # #85 nudge_weight/get_weights need a shared `now` so read-time reversion doesn't drift the
    # assertion between the write and the read (they'd otherwise both default to real wall-clock time).
    import pytest
    dao = store.rec_model
    assert dao.get_weights(now=1000.0) == {}              # missing axis = prior 1.0 (empty map)
    up = dao.nudge_weight("lane:deep_cut", 2.0, now=1000.0)
    assert 1.0 < up <= 3.0 and dao.get_weights(now=1000.0)["lane:deep_cut"] == pytest.approx(up)
    dao.set_weight("lane:neighbourhood", 0.5, now=1000.0)
    assert dao.get_weights(now=1000.0)["lane:neighbourhood"] == pytest.approx(0.5)
    dao.reset_weights()
    assert dao.get_weights(now=1000.0) == {}


def test_feedback_suppression_and_mutes(store):
    dao = store.rec_model
    dao.record_feedback("for_you", "k1", "dismiss", now=100.0)
    dao.record_feedback("for_you", "k2", "not_now", until=200.0, now=100.0)
    dao.record_feedback("for_you", "artist:Foo", "mute", now=100.0)
    assert dao.suppressed_keys("for_you", now=150.0) == {"k1", "k2", "artist:Foo"}
    assert dao.suppressed_keys("for_you", now=250.0) == {"k1", "artist:Foo"}   # k2's snooze expired
    assert dao.muted_artists() == {"Foo"}


def test_vectors_roundtrip(store):
    dao = store.rec_model
    assert dao.rec_vectors_count() == 0
    dao.replace_rec_vectors([("a", b"\x00\x01"), ("b", b"\x02\x03")])
    assert dao.rec_vectors_count() == 2
    assert dict(dao.get_rec_vectors()) == {"a": b"\x00\x01", "b": b"\x02\x03"}
    dao.replace_rec_vectors([])                          # wholesale replace
    assert dao.rec_vectors_count() == 0


def test_model_state_is_separate_from_library_queries(store):
    import pytest
    store.rec_model.set_weight("lane:rotation", 1.2)
    # Use pytest.approx to handle floating point precision from reversion calculations
    assert store.rec_model.get_weights()["lane:rotation"] == pytest.approx(1.2)
    assert store.rec_model.rec_vectors_count() == 0


def test_track_decades_and_artists_and_era_distribution(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.tracks.upsert_track("v1", "A", "Alpha", None, None)   # key "a|alpha"
    b = store.tracks.upsert_track("v2", "B", "Beta", None, None)    # key "b|beta"
    store.tracks.set_track_year(a, "1991")                          # -> decade "1990"
    store.tracks.set_track_year(b, "2003")                          # -> decade "2000"
    store.history.record_history_plays(iid, 1.0, ["a|alpha"], source_timezone="UTC")

    decades = store.rec_query.track_decades(["a|alpha", "b|beta"])
    assert decades == {"a|alpha": "1990", "b|beta": "2000"}
    artists = store.rec_query.track_artists(["a|alpha", "b|beta"])
    assert artists == {"a|alpha": "Alpha", "b|beta": "Beta"}

    dist = store.rec_query.era_play_distribution()
    assert dist["1990"] == 2     # 1 + 1 play
    assert dist["2000"] == 1     # 1 + 0 plays
