import pytest


@pytest.fixture
def store(store):
    store.accounts.upsert_account("me", "c", None, True)   # identity id=1, referenced by history/playlist FKs
    return store


def _snap(store, day, keys):
    """Record independent observations on an explicit UTC calendar date."""
    store.history.record_history_plays(1, day * 86400.0, keys, source_timezone="UTC")


def _track(store, key, artist, genre=""):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store,key,"T"+key,artist,video_id="v"+key,genre=genre)


def test_play_day_counts_and_meta(store):
    _track(store, "k1", "A1", "house")
    _track(store, "k3", "A2", "techno")
    _snap(store, 2, ["k1", "k3"])
    _snap(store, 5, ["k1"])
    # (day, key, count): day2 has k1 and k3 once each; day5 has k1 once
    assert sorted(store.trends.play_day_counts()) == [(2, "k1", 1), (2, "k3", 1), (5, "k1", 1)]
    artists = {row['name']:'@artist:'+str(row['id']) for row in store.conn.execute('SELECT id,name FROM artists')}
    assert store.trends.track_meta() == {"k1": ((artists['A1'],), "House"), "k3": ((artists['A2'],), "Techno")}


def test_track_cards_and_month_track_plays(store):
    _track(store, "k1", "A1", "house")               # ensure the tracks row carries title/thumbnail
    store.conn.execute("UPDATE youtube_items SET thumbnail='http://x/1.jpg', album_browse_id='MPRE1' "
                       "WHERE video_id='vk1'")
    store.conn.commit()
    _snap(store, 2, ["k1"]); _snap(store, 5, ["k1"])
    cards = store.trends.track_cards(["k1"])
    assert cards["k1"]["thumbnail"] == "http://x/1.jpg" and cards["k1"]["album_browse_id"] == "MPRE1"
    # month window covering days 2 and 5 -> k1 played twice
    since, until = 0.0, 40 * 86400.0
    assert store.trends.month_track_plays(since, until) == {"k1": 2}
