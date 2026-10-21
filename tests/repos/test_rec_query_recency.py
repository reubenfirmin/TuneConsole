from yt_playlist.util.matching import identity_key


def test_track_last_played_returns_newest_ts_per_key(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    k1, k2 = identity_key("S1", "A"), identity_key("S2", "A")
    store.history.add_history_snapshot(iid, 100.0, [k1, k2])
    store.history.add_history_snapshot(iid, 200.0, [k1])           # k1 heard again, later
    out = store.rec_query.track_last_played([k1, k2, identity_key("never", "X")])
    assert out[k1] == 200.0          # newest snapshot wins
    assert out[k2] == 100.0
    assert identity_key("never", "X") not in out           # no history -> absent
