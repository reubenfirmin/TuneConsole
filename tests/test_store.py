# tests/test_store.py
def test_upsert_playlist_tracks_seen_and_changed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t1 = store.upsert_track("v1", "Song A", "Artist", "Alb", 200)
    t2 = store.upsert_track("v2", "Song B", "Artist", "Alb", 210)
    pid = store.upsert_playlist(iid, "PL1", "Mix", 2, "hash1", 1000.0)
    store.set_playlist_tracks(pid, [t1, t2])

    assert store.get_playlist_track_keys(pid) == {"song a|artist", "song b|artist"}

    # re-sync, same hash -> last_changed unchanged, last_seen updated
    store.upsert_playlist(iid, "PL1", "Mix", 2, "hash1", 2000.0)
    pl = [p for p in store.get_playlists() if p.ytm_playlist_id == "PL1"][0]
    assert pl.first_seen == 1000.0
    assert pl.last_seen == 2000.0
    assert pl.last_changed == 1000.0

    # re-sync, new hash -> last_changed bumps
    store.upsert_playlist(iid, "PL1", "Mix", 3, "hash2", 3000.0)
    pl = [p for p in store.get_playlists() if p.ytm_playlist_id == "PL1"][0]
    assert pl.last_changed == 3000.0

def test_upsert_identity_is_idempotent(store):
    a = store.upsert_identity("main", "cred", None, True)
    b = store.upsert_identity("main", "cred2", "B1", True)  # same label
    assert a == b
    ids = store.get_identities()
    assert len(ids) == 1
    assert ids[0].credential_ref == "cred2"
    assert ids[0].brand_account_id == "B1"

def test_set_playlist_tracks_replace_semantics(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t1 = store.upsert_track("v1", "Song A", "Artist", "Alb", 200)
    t2 = store.upsert_track("v2", "Song B", "Artist", "Alb", 210)
    t3 = store.upsert_track("v3", "Song C", "Artist", "Alb", 220)
    pid = store.upsert_playlist(iid, "PL1", "Mix", 3, "hash1", 1000.0)
    store.set_playlist_tracks(pid, [t1, t2])
    assert store.get_playlist_track_keys(pid) == {"song a|artist", "song b|artist"}
    # Second call replaces the membership entirely
    store.set_playlist_tracks(pid, [t2, t3])
    keys = store.get_playlist_track_keys(pid)
    assert keys == {"song b|artist", "song c|artist"}
    assert len(keys) == 2

def test_upsert_track_null_video_id_dedup(store):
    store.upsert_track(None, "T", "X", None, None)
    store.upsert_track(None, "T", "X", None, None)
    rows = store.conn.execute(
        "SELECT COUNT(*) AS cnt FROM tracks WHERE identity_key=?",
        ("t|x",)).fetchone()
    assert rows["cnt"] == 1

def test_history_keys_window(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.add_history_snapshot(iid, 5000.0, ["song a|artist", "song c|artist"])
    assert store.get_recent_history_keys(4000.0) == {"song a|artist", "song c|artist"}
    assert store.get_recent_history_keys(6000.0) == set()

def test_get_action_roundtrip(store):
    aid = store.record_action("plan", '{"a":1}', '{"b":2}', "planned", "{}", 10.0)
    a = store.get_action(aid)
    assert a.id == aid and a.kind == "plan" and a.status == "planned"
    assert a.params_json == '{"a":1}' and a.plan_json == '{"b":2}'
    assert store.get_action(99999) is None

def test_update_action_sets_undo(store):
    aid = store.record_action("plan", "{}", "{}", "planned", "{}", 10.0)
    store.update_action(aid, "executed", 20.0, undo_json='{"backup":"/x.json"}')
    a = store.get_action(aid)
    assert a.status == "executed" and a.executed_at == 20.0
    assert a.undo_json == '{"backup":"/x.json"}'
    # undo_json omitted -> left unchanged
    store.update_action(aid, "cancelled", 30.0)
    assert store.get_action(aid).undo_json == '{"backup":"/x.json"}'

def test_get_playlist_tracks_with_meta_includes_duration(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Artist", None, 207, True)
    pid = store.upsert_playlist(iid, "PL", "p", 1, "h", 1.0)
    store.set_playlist_tracks(pid, [t])
    rows = store.get_playlist_tracks_with_meta(pid)
    assert rows == [("song|artist", "v1", "Song", "Artist", 207, 1)]  # trailing field = available


def test_concurrent_access_is_serialized(store):
    # FastAPI serves sync routes from a threadpool, so the shared connection is hit from many
    # threads. Without the Store lock this raises sqlite3 ProgrammingError / corrupts state.
    import threading
    store.upsert_identity("main", "cred", None, True)
    errors = []

    def worker(n):
        try:
            for i in range(25):
                tid = store.upsert_track(f"v{n}_{i}", f"Song {n} {i}", "Artist", "Alb", 200)
                pid = store.upsert_playlist(1, f"PL{n}_{i}", "p", 1, f"h{n}_{i}", 1.0)
                store.set_playlist_tracks(pid, [tid])
                store.get_playlists()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.get_playlists()) == 8 * 25


def test_suppress_overlap_roundtrip(store):
    store.suppress_overlap("PLB", "PLA", 1.0)
    assert store.get_suppressed_overlap_pairs() == {frozenset(("PLA", "PLB"))}
    store.suppress_overlap("PLA", "PLB", 2.0)
    assert len(store.get_suppressed_overlaps()) == 1
    store.unsuppress_overlap("PLA", "PLB")
    assert store.get_suppressed_overlap_pairs() == set()
