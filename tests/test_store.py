# tests/test_store.py
def test_play_provenance_upgrade_preserves_unknown_history_and_reopens(tmp_path, legacy_store):
    from yt_playlist.core.store import Store

    store = legacy_store
    path = tmp_path / "legacy.db"
    iid = 1
    with store.conn:
        # Current repositories require account scopes and provenance tables absent in v7.
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (?,'main','cred',1)", (iid,))
        store.conn.execute("INSERT INTO playlists(identity_id,ytm_playlist_id,title) VALUES (?,'PLGEN','Generated')", (iid,))
        store.conn.execute("INSERT INTO playlist_group VALUES ('PLGEN','Generated')")
        store.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key) VALUES ('vid','Song','Artist','song|artist')")
        store.conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at,playlist_ytm_id) VALUES (?,'song|artist','vid',120,'RDAMPLPLGEN')", (iid,))
        store.conn.execute("ALTER TABLE play_events DROP COLUMN is_generated")
        store.conn.execute("DROP TABLE schema_migrations")
    store.conn.close()

    store = Store(path)
    store.init_schema()
    assert store.conn.execute("SELECT is_generated FROM play_events").fetchone()[0] is None
    store.tracks.upsert_track("new", "New", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("new")
    store.history.record_play_event(iid, key, "new", 5000, playlist_ytm_id="PLGEN")
    store.playlists.set_playlist_group("PLGEN", "")
    store.conn.close()

    store = Store(path)
    store.init_schema()
    assert [r[0] for r in store.conn.execute("SELECT is_generated FROM play_events ORDER BY id")] == [None, 1]
    assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.conn.close()


def test_retired_source_weights_are_removed_and_not_restored(legacy_store):
    store = legacy_store
    store.rec_model.set_weight("lane:explore", 1.3, now=1000)
    store.rec_model.set_weight("lane:deep_cut", 1.7, now=900)
    store.rec_model.set_weight("genre:jazz", 0, lo=0, now=800)
    expected = {axis: row for axis, row in store.rec_model.weight_records().items() if axis != "lane:explore"}
    param_keys = ("rec_param:breadth_bias",)
    store.rec_model.save_taste_settings("old-source-checkpoint", param_keys, 1000)
    with store.conn:
        store.conn.execute("DROP TABLE schema_migrations")

    store.init_schema()
    assert store.rec_model.weight_records() == expected
    assert store.rec_model.restore_taste_settings("old-source-checkpoint", param_keys)
    assert store.rec_model.weight_records() == expected
    store.init_schema()
    assert store.rec_model.weight_records() == expected


def test_playlist_creation_date_migration_backfills_only_known_dates(legacy_store):
    store = legacy_store
    import json
    from yt_playlist.util.action_kinds import COPY_PLAYLIST, MOVE_IDENTITY

    # Recreate the previous schema, then exercise the real startup migration.
    with store.conn:
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','cred',1)")
        for ytm in ("song", "generated", "303", "Brexit List", "planned", "moved"):
            store.conn.execute("INSERT INTO playlists(identity_id,ytm_playlist_id,title,track_count,content_hash,first_seen,last_seen,last_changed) VALUES (1,?,?,0,'',9000,9000,9000)", (ytm, ytm))
        for kind, ytm, status, at in ((COPY_PLAYLIST, "song", "executed", 100),
                                      (COPY_PLAYLIST, "song", "executed", 200),
                                      (COPY_PLAYLIST, "planned", "planned", 300),
                                      (MOVE_IDENTITY, "moved", "executed", 400)):
            store.conn.execute("INSERT INTO actions(kind,params_json,plan_json,status,undo_json,created_at) VALUES (?,'{}','{}',?,?,?)", (kind, status, json.dumps({"new_ytm": ytm}), at))
        store.conn.execute("INSERT INTO actions(kind,status,undo_json,created_at) VALUES (?,'executed','invalid legacy payload',500)", (COPY_PLAYLIST,))
        store.conn.execute("INSERT INTO rec_recipes VALUES ('generated',?,50)", (json.dumps({"theme": "mix"}),))
        store.conn.execute("ALTER TABLE playlists DROP COLUMN created_at")
        store.conn.execute("DELETE FROM settings WHERE key='playlist_created_at_backfilled'")
        store.conn.execute("DROP TABLE schema_migrations")
    store.init_schema()
    dates = {p.ytm_playlist_id: p.created_at for p in store.playlists.get_playlists()}
    assert dates == {"song": 100, "generated": 50, "303": None, "Brexit List": None,
                     "planned": None, "moved": None}
    store.playlist_recipes.set_recipe("generated", {"theme": "updated mix"}, 8000)
    store.init_schema()
    assert {p.ytm_playlist_id: p.created_at for p in store.playlists.get_playlists()} == dates


def test_upsert_playlist_tracks_seen_and_changed(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t1 = store.tracks.upsert_track("v1", "Song A", "Artist", "Alb", 200)
    t2 = store.tracks.upsert_track("v2", "Song B", "Artist", "Alb", 210)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "hash1", 1000.0)
    store.playlists.set_playlist_tracks(pid, [t1, t2])

    assert store.playlists.get_playlist_track_keys(pid) == {"song a|artist", "song b|artist"}

    # re-sync, same hash -> last_changed unchanged, last_seen updated
    store.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "hash1", 2000.0)
    pl = [p for p in store.playlists.get_playlists() if p.ytm_playlist_id == "PL1"][0]
    assert pl.first_seen == 1000.0
    assert pl.last_seen == 2000.0
    assert pl.last_changed == 1000.0

    # re-sync, new hash -> last_changed bumps
    store.playlists.upsert_playlist(iid, "PL1", "Mix", 3, "hash2", 3000.0)
    pl = [p for p in store.playlists.get_playlists() if p.ytm_playlist_id == "PL1"][0]
    assert pl.last_changed == 3000.0

def test_upsert_identity_is_idempotent(store):
    a = store.identities.upsert_identity("main", "cred", "B1", True)
    b = store.identities.upsert_identity("main", "cred2", "B1", True)  # same account, rotated credential
    assert a == b
    ids = store.identities.get_identities()
    assert len(ids) == 1
    assert ids[0].credential_ref == "cred2"
    assert ids[0].brand_account_id == "B1"

def test_set_playlist_tracks_replace_semantics(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t1 = store.tracks.upsert_track("v1", "Song A", "Artist", "Alb", 200)
    t2 = store.tracks.upsert_track("v2", "Song B", "Artist", "Alb", 210)
    t3 = store.tracks.upsert_track("v3", "Song C", "Artist", "Alb", 220)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 3, "hash1", 1000.0)
    store.playlists.set_playlist_tracks(pid, [t1, t2])
    assert store.playlists.get_playlist_track_keys(pid) == {"song a|artist", "song b|artist"}
    # Second call replaces the membership entirely
    store.playlists.set_playlist_tracks(pid, [t2, t3])
    keys = store.playlists.get_playlist_track_keys(pid)
    assert keys == {"song b|artist", "song c|artist"}
    assert len(keys) == 2

def test_upsert_track_null_video_id_dedup(store):
    store.tracks.upsert_track(None, "T", "X", None, None)
    store.tracks.upsert_track(None, "T", "X", None, None)
    rows = store.conn.execute(
        "SELECT COUNT(*) AS cnt FROM tracks WHERE identity_key=?",
        ("t|x",)).fetchone()
    assert rows["cnt"] == 1

def test_upsert_track_backfills_duration_on_resync(store):
    # A track can enter the store without a duration (history/plays sync inserts duration_s=None).
    # Re-upserting it later WITH a duration, e.g. adding it via "find alternate version", must
    # backfill duration_s, otherwise its time never shows in the playlist (issue #26).
    t = store.tracks.upsert_track("v1", "Song A", "Artist X", "Alb", None, 1)
    again = store.tracks.upsert_track("v1", "Song A", "Artist X", "Alb", 250, 1)
    assert again == t  # same row reused, not a duplicate
    dur = store.conn.execute("SELECT duration_s FROM tracks WHERE id=?", (t,)).fetchone()["duration_s"]
    assert dur == 250

def test_history_keys_window(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    day = 86400
    store.history.record_history_plays(iid, 5 * day, ["song a|artist", "song c|artist"], source_timezone="UTC")
    assert store.history.get_recent_history_keys(4 * day) == {"song a|artist", "song c|artist"}
    assert store.history.get_recent_history_keys(6 * day) == set()

def test_get_action_roundtrip(store):
    aid = store.actions.record_action("plan", '{"a":1}', '{"b":2}', "planned", "{}", 10.0)
    a = store.actions.get_action(aid)
    assert a.id == aid and a.kind == "plan" and a.status == "planned"
    assert a.params_json == '{"a":1}' and a.plan_json == '{"b":2}'
    assert store.actions.get_action(99999) is None

def test_update_action_sets_undo(store):
    aid = store.actions.record_action("plan", "{}", "{}", "planned", "{}", 10.0)
    store.actions.update_action(aid, "executed", 20.0, undo_json='{"backup":"/x.json"}')
    a = store.actions.get_action(aid)
    assert a.status == "executed" and a.executed_at == 20.0
    assert a.undo_json == '{"backup":"/x.json"}'
    # undo_json omitted -> left unchanged
    store.actions.update_action(aid, "cancelled", 30.0)
    assert store.actions.get_action(aid).undo_json == '{"backup":"/x.json"}'

def test_get_playlist_tracks_with_meta_includes_duration(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Artist", None, 207, True)
    pid = store.playlists.upsert_playlist(iid, "PL", "p", 1, "h", 1.0)
    store.playlists.set_playlist_tracks(pid, [t])
    rows = store.playlists.get_playlist_tracks_with_meta(pid)
    assert rows == [("song|artist", "v1", "Song", "Artist", 207, 1)]  # trailing field = available


def test_concurrent_access_is_serialized(store):
    # FastAPI serves sync routes from a threadpool, so the shared connection is hit from many
    # threads. Without the Store lock this raises sqlite3 ProgrammingError / corrupts state.
    import threading
    store.identities.upsert_identity("main", "cred", None, True)
    errors = []

    def worker(n):
        try:
            for i in range(25):
                tid = store.tracks.upsert_track(f"v{n}_{i}", f"Song {n} {i}", "Artist", "Alb", 200)
                pid = store.playlists.upsert_playlist(1, f"PL{n}_{i}", "p", 1, f"h{n}_{i}", 1.0)
                store.playlists.set_playlist_tracks(pid, [tid])
                store.playlists.get_playlists()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.playlists.get_playlists()) == 8 * 25


def test_suppress_overlap_roundtrip(store):
    store.overlaps.suppress_overlap("PLB", "PLA", 1.0)
    assert store.overlaps.get_suppressed_overlap_pairs() == {frozenset(("PLA", "PLB"))}
    store.overlaps.suppress_overlap("PLA", "PLB", 2.0)
    assert len(store.overlaps.get_suppressed_overlaps()) == 1
    store.overlaps.unsuppress_overlap("PLA", "PLB")
    assert store.overlaps.get_suppressed_overlap_pairs() == set()


def test_theme_rows_returns_all_facets(store):
    store.rec_signals.bump_theme("genre:jazz", 0.4, 1000.0)
    store.rec_signals.bump_theme("era:1970", -0.3, 1000.0)
    rows = {r["facet"]: r["score"] for r in store.rec_signals.theme_rows()}
    assert abs(rows["genre:jazz"] - 0.4) < 1e-9
    assert abs(rows["era:1970"] - (-0.3)) < 1e-9
    # strongest magnitude first
    assert store.rec_signals.theme_rows()[0]["facet"] == "genre:jazz"


def test_orig_title_artist_columns_exist_and_backfill(store):
    # a fresh insert carries the originals; reset relies on these columns.
    t = store.tracks.upsert_track("v1", "Bad Title", "Bad Artist", "Al", 100)
    row = store.conn.execute(
        "SELECT title, artist, orig_title, orig_artist FROM tracks WHERE id=?", (t,)).fetchone()
    assert row["orig_title"] == "Bad Title"
    assert row["orig_artist"] == "Bad Artist"
