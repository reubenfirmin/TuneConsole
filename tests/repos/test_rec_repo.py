"""DAO suite for RecRepo (recommendation persistence: impressions, proposals, taste queries).

Confirms the recs DAO is unified onto the shared Repo base — it binds the Store's connection +
lock (constructed as RecRepo(store)) and owns its own rec tables. RecDao is kept as an alias.
"""
from yt_playlist.repos.base import Repo
from yt_playlist.repos.rec import RecRepo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.repos.rec_surface import RecSurfaceRepo


def test_recrepo_is_a_facade_over_three_focused_daos(store):
    # The former 40-method god class is split by responsibility; RecRepo composes the parts and
    # delegates, so model/surface/query methods all resolve through the one facade object.
    assert isinstance(store.rec.model, RecModelRepo)        # learned model
    assert isinstance(store.rec.surface, RecSurfaceRepo)    # serving surfaces
    assert isinstance(store.rec.query, RecQueryRepo)        # library reads + candidate generators
    store.rec.set_weight("lane:explore", 1.1)               # -> model, via facade __getattr__
    assert store.rec.get_weights()["lane:explore"] == 1.1
    assert store.rec.tracks_total() == 0                    # -> query, via facade __getattr__
    assert store.rec.get_proposals("discover") is None      # -> surface, via facade __getattr__


def test_unified_onto_repo_base(store):
    dao = RecRepo(store)
    assert isinstance(dao, Repo)
    assert dao.conn is store.conn and dao._lock is store._lock   # shares the Store's conn + lock


def test_rec_dao_alias(store):
    from yt_playlist.rec_dao import RecDao
    assert RecDao is RecRepo


def test_card_view_counter_ticks_and_reads(store):
    dao = RecRepo(store)
    assert dao.card_views("wheelhouse") == 0                      # never shown
    assert dao.bump_card_view("wheelhouse", now=1000.0) == 1      # first tick
    assert dao.bump_card_view("wheelhouse", now=1100.0) == 2
    assert dao.card_views("wheelhouse") == 2                      # read-only, doesn't advance
    assert dao.card_views("wheelhouse") == 2
    assert dao.bump_card_view("comfort", now=1000.0) == 1         # independent per card
    assert dao.card_views("wheelhouse") == 2


def test_proposals_roundtrip_and_missing(store):
    dao = RecRepo(store)
    assert dao.get_proposals("discover") is None
    dao.put_proposals("discover", [{"artist": "X", "title": "LP"}], now=1.0)
    assert dao.get_proposals("discover") == [{"artist": "X", "title": "LP"}]


def test_tracks_total_replaces_raw_sql(store):
    dao = RecRepo(store)
    assert dao.tracks_total() == store.conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]


# --- the learned-model methods folded in from the former god class ---

def test_weights_nudge_set_reset(store):
    dao = RecRepo(store)
    assert dao.get_weights() == {}                       # missing axis = prior 1.0 (empty map)
    up = dao.nudge_weight("lane:deep_cut", 2.0)
    assert 1.0 < up <= 3.0 and dao.get_weights()["lane:deep_cut"] == up
    dao.set_weight("lane:explore", 0.5)
    assert dao.get_weights()["lane:explore"] == 0.5
    dao.reset_weights()
    assert dao.get_weights() == {}


def test_feedback_suppression_and_mutes(store):
    dao = RecRepo(store)
    dao.record_feedback("for_you", "k1", "dismiss", now=100.0)
    dao.record_feedback("for_you", "k2", "not_now", until=200.0, now=100.0)
    dao.record_feedback("for_you", "artist:Foo", "mute", now=100.0)
    assert dao.suppressed_keys("for_you", now=150.0) == {"k1", "k2", "artist:Foo"}
    assert dao.suppressed_keys("for_you", now=250.0) == {"k1", "artist:Foo"}   # k2's snooze expired
    assert dao.muted_artists() == {"Foo"}


def test_vectors_roundtrip(store):
    dao = RecRepo(store)
    assert dao.rec_vectors_count() == 0
    dao.replace_rec_vectors([("a", b"\x00\x01"), ("b", b"\x02\x03")])
    assert dao.rec_vectors_count() == 2
    assert dict(dao.get_rec_vectors()) == {"a": b"\x00\x01", "b": b"\x02\x03"}
    dao.replace_rec_vectors([])                          # wholesale replace
    assert dao.rec_vectors_count() == 0


def test_folded_methods_delegate_via_facade(store):
    store.set_weight("lane:rotation", 1.2)               # legacy store.x() call site
    assert store.get_weights()["lane:rotation"] == 1.2
    assert store.rec_vectors_count() == 0


def test_track_decades_and_artists_and_era_distribution(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "A", "Alpha", None, None)   # key "a|alpha"
    b = store.upsert_track("v2", "B", "Beta", None, None)    # key "b|beta"
    store.set_track_year(a, "1991")                          # -> decade "1990"
    store.set_track_year(b, "2003")                          # -> decade "2000"
    store.add_history_snapshot(iid, 1.0, ["a|alpha"])        # one play of A

    decades = store.track_decades(["a|alpha", "b|beta"])
    assert decades == {"a|alpha": "1990", "b|beta": "2000"}
    artists = store.track_artists(["a|alpha", "b|beta"])
    assert artists == {"a|alpha": "Alpha", "b|beta": "Beta"}

    dist = store.era_play_distribution()
    assert dist["1990"] == 2     # 1 + 1 play
    assert dist["2000"] == 1     # 1 + 0 plays
