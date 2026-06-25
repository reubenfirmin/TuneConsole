import json

from yt_playlist.rec import autotune_run


def _seed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(40)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(40)]
    for j in range(6):
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PA{j}", "PA", 8, f"ha{j}", 0.0), A[j*5:j*5+8])
        store.set_playlist_tracks(store.upsert_playlist(iid, f"PB{j}", "PB", 8, f"hb{j}", 0.0), B[j*5:j*5+8])


def test_run_and_record_persists_result(store):
    _seed(store)
    res = autotune_run.run_and_record(store, now=1000.0)
    assert res["winner"]["dim"] in (48, 64, 96, 128)
    assert set(res["recs"]) == {"dropped", "added", "compared"}
    assert res["ran_at"] == 1000.0
    # round-trips through the setting
    stored = json.loads(store.get_setting("rec_autotune_result"))
    assert stored["winner"] == res["winner"]
    assert autotune_run.last_result(store)["ran_at"] == 1000.0


def test_last_result_none_when_absent(store):
    assert autotune_run.last_result(store) is None
