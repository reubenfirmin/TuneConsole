import pytest
from yt_playlist.core.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    s.upsert_identity("me", "c", None, True)   # identity id=1, referenced by history/playlist FKs
    return s


def _snap(store, day, keys):
    """One history snapshot at taken_at = day*86400 containing `keys`. Returns snapshot id."""
    cur = store.conn.execute("INSERT INTO history_snapshots(identity_id, taken_at) VALUES (1, ?)",
                             (day * 86400.0,))
    sid = cur.lastrowid
    for k in keys:
        store.conn.execute("INSERT INTO history_items(snapshot_id, identity_key) VALUES (?, ?)", (sid, k))
    store.conn.commit()
    return sid


def _track(store, key, artist, genre=""):
    store.conn.execute(
        "INSERT INTO tracks(identity_key, video_id, title, artist, genre) VALUES (?,?,?,?,?)",
        (key, "v" + key, "T" + key, artist, genre))
    store.conn.commit()


def _track_id(store, key):
    return store.conn.execute("SELECT id FROM tracks WHERE identity_key = ?", (key,)).fetchone()["id"]


def test_play_day_counts_and_meta(store):
    _track(store, "k1", "A1", "house")
    _track(store, "k3", "A2", "techno")
    _snap(store, 2, ["k1", "k3"])
    _snap(store, 5, ["k1"])
    # (day, key, count): day2 has k1 and k3 once each; day5 has k1 once
    assert sorted(store.trends.play_day_counts()) == [(2, "k1", 1), (2, "k3", 1), (5, "k1", 1)]
    assert store.trends.track_meta() == {"k1": ("A1", "house"), "k3": ("A2", "techno")}


def test_never_played_and_last(store):
    _track(store, "k1", "A1", "house")
    _track(store, "k2", "A1", "house")     # never appears in a snapshot
    _snap(store, 5, ["k1"])
    # 2 tracks total, k2 never played -> 1
    assert store.trends.never_played() == (2, 1)
    last = dict(store.trends.track_last_play())
    assert last["k1"] == 5 * 86400.0 and last["k2"] is None


def test_dead_playlists_threshold_and_order(store):
    _track(store, "ka", "A1")
    _track(store, "kb", "A2")
    _track(store, "kc", "A3")

    pid_zeta = store.conn.execute(
        "INSERT INTO playlists(identity_id, ytm_playlist_id, title) VALUES (1, ?, ?)",
        ("pl_zeta", "Zeta")).lastrowid
    pid_alpha = store.conn.execute(
        "INSERT INTO playlists(identity_id, ytm_playlist_id, title) VALUES (1, ?, ?)",
        ("pl_alpha", "Alpha")).lastrowid
    pid_beta = store.conn.execute(
        "INSERT INTO playlists(identity_id, ytm_playlist_id, title) VALUES (1, ?, ?)",
        ("pl_beta", "Beta")).lastrowid
    store.conn.commit()

    store.conn.execute(
        "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?, ?, 0)",
        (pid_zeta, _track_id(store, "ka")))
    store.conn.execute(
        "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?, ?, 0)",
        (pid_alpha, _track_id(store, "kb")))
    store.conn.execute(
        "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?, ?, 0)",
        (pid_beta, _track_id(store, "kc")))
    store.conn.commit()

    # kc (Beta's only track) played 3 times: day2, day5, day7. ka/kb (Zeta/Alpha) never played.
    _snap(store, 2, ["kc"])
    _snap(store, 5, ["kc"])
    _snap(store, 7, ["kc"])

    # max_listens=1: Beta (listens=3) excluded; Zeta and Alpha (listens=0 each) tie, broken by
    # case-insensitive title -> Alpha before Zeta.
    low = store.trends.dead_playlists(1)
    assert [(d["title"], d["listens"], d["last_listen"]) for d in low] == [
        ("Alpha", 0, None), ("Zeta", 0, None)]

    # max_listens=3: all three included, listens ascending then title.
    full = store.trends.dead_playlists(3)
    assert [(d["title"], d["listens"]) for d in full] == [("Alpha", 0), ("Zeta", 0), ("Beta", 3)]
    beta = next(d for d in full if d["title"] == "Beta")
    assert beta["last_listen"] == 7 * 86400.0
    assert beta["playlist_id"] == pid_beta


def test_track_cards_and_month_track_plays(store):
    _track(store, "k1", "A1", "house")               # ensure the tracks row carries title/thumbnail
    store.conn.execute("UPDATE tracks SET thumbnail='http://x/1.jpg', album_browse_id='MPRE1' "
                       "WHERE identity_key='k1'")
    store.conn.commit()
    _snap(store, 2, ["k1"]); _snap(store, 5, ["k1"])
    cards = store.trends.track_cards(["k1"])
    assert cards["k1"]["thumbnail"] == "http://x/1.jpg" and cards["k1"]["album_browse_id"] == "MPRE1"
    # month window covering days 2 and 5 -> k1 played twice
    since, until = 0.0, 40 * 86400.0
    assert store.trends.month_track_plays(since, until) == {"k1": 2}


def test_rediscover_tracks_old_favorites(store):
    _track(store, "hot", "A1"); _track(store, "cold", "A2")
    # cold: 3 plays, newest day 5. hot: 1 play, newest day 100.
    for d in (1, 3, 5):
        _snap(store, d, ["cold"])
    _snap(store, 100, ["hot"])
    # before_ts = day 40 -> only 'cold' (last day 5 < 40) qualifies; 'hot' (day 100) excluded.
    got = store.trends.rediscover_tracks(before_ts=40 * 86400.0, limit=3)
    assert [r["identity_key"] for r in got] == ["cold"] and got[0]["plays"] == 3
