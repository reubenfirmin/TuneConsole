# tests/test_store.py
from tests.historical_support import upgrade_fixture


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
    upgrade_fixture(store)
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
    store.rec_model.save_taste_settings("taste_settings_checkpoint_v1", param_keys, 1000)
    with store.conn:
        store.conn.execute("DROP TABLE schema_migrations")

    upgrade_fixture(store)
    assert store.rec_model.weight_records() == expected
    assert store.rec_model.restore_taste_settings("taste_settings_checkpoint_v1", param_keys)
    assert store.rec_model.weight_records() == expected
    store.init_schema()
    assert store.rec_model.weight_records() == expected


def test_playlist_creation_date_migration_backfills_only_known_dates(legacy_store):
    store = legacy_store
    import json
    from yt_playlist.core.migrations import migrate
    from yt_playlist.core.migrations.registry import MIGRATIONS
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
    # Replay the creation-date migration on its frozen schema. The intentionally
    # invalid action payload tests v4's tolerant date lookup, not v98 admission.
    migrate(store.conn, registry=MIGRATIONS[:7])
    dates = dict(store.conn.execute('SELECT ytm_playlist_id,created_at FROM playlists'))
    assert dates == {"song": 100, "generated": 50, "303": None, "Brexit List": None,
                     "planned": None, "moved": None}
    with store.conn:
        store.conn.execute('UPDATE rec_recipes SET recipe=?,created_at=8000 WHERE playlist_ytm=?',
                           (json.dumps({"theme": "updated mix"}), "generated"))
    migrate(store.conn, registry=MIGRATIONS[:7])
    assert dict(store.conn.execute('SELECT ytm_playlist_id,created_at FROM playlists')) == dates


def test_upsert_playlist_tracks_seen_and_changed(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    t1 = store.tracks.upsert_track("v1", "Song A", "Artist", "Alb", 200)
    t2 = store.tracks.upsert_track("v2", "Song B", "Artist", "Alb", 210)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "hash1", 1000.0)
    store.playlists.set_playlist_tracks(pid, [t1, t2])

    assert store.playlists.get_playlist_track_keys(pid) == {"song a|artist", "song b|artist"}

    # re-sync, same hash -> last_changed unchanged, last_seen updated
    store.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "hash1", 2000.0)
    pl = [p for p in store.playlists.get_playlists() if p.provider_playlist_id == "PL1"][0]
    assert pl.first_seen == 1000.0
    assert pl.last_seen == 2000.0
    assert pl.last_changed == 1000.0

    # re-sync, new hash -> last_changed bumps
    store.playlists.upsert_playlist(iid, "PL1", "Mix", 3, "hash2", 3000.0)
    pl = [p for p in store.playlists.get_playlists() if p.provider_playlist_id == "PL1"][0]
    assert pl.last_changed == 3000.0

def test_upsert_identity_is_idempotent(store):
    a = store.accounts.upsert_account("main", "cred", "B1", True)
    b = store.accounts.upsert_account("main", "cred2", "B1", True)  # same account, rotated credential
    assert a == b
    ids = store.accounts.get_accounts()
    assert len(ids) == 1
    assert ids[0].credential_ref == "cred2"
    assert ids[0].brand_account_id == "B1"

def test_set_playlist_tracks_replace_semantics(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
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

def test_get_action_roundtrip(store):
    import json

    aid = store.actions.record_action("plan", '{"a":1}', '{"b":2}', "planned", "{}", 10.0)
    a = store.actions.get_action(aid)
    assert a.id == aid and a.kind == "plan" and a.status == "planned"
    assert json.loads(a.params_json) == {'a': 1, 'mode': None}
    assert json.loads(a.plan_json) == {'b': 2}
    assert store.actions.get_action(99999) is None

def test_update_action_sets_undo(store, tmp_path):
    import json
    from yt_playlist.core import fao

    backup = tmp_path / 'backup.json'
    fao.write(backup, json.dumps({'ytm_playlist_id': 'PL', 'tracks': []}).encode())
    aid = store.actions.record_action("plan", "{}", "{}", "planned", "{}", 10.0)
    store.actions.update_action(aid, "executed", 20.0, undo_json=json.dumps({'backup': str(backup)}))
    a = store.actions.get_action(aid)
    assert a.status == "executed" and a.executed_at == 20.0
    retained = json.loads(a.undo_json)['backup']
    assert fao.read(retained).bytes == fao.read(backup).bytes
    # An admitted same-state update preserves Undo when that argument is omitted.
    store.actions.update_action(aid, "executed", 30.0)
    assert store.actions.get_action(aid).undo_json == a.undo_json
    assert store.actions.get_action(aid).executed_at == 30.0

def test_get_playlist_tracks_with_meta_includes_duration(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Artist", None, 207, True)
    pid = store.playlists.upsert_playlist(iid, "PL", "p", 1, "h", 1.0)
    store.playlists.set_playlist_tracks(pid, [t])
    rows = store.playlists.get_playlist_tracks_with_meta(pid)
    assert rows == [("song|artist", "v1", "Song", "Artist", 207, 1)]  # trailing field = available


def _assert_shared_store_sql_owned(statements):
    assert statements, "No shared-connection SQL was observed"
    unowned = [(name, sql) for name, owned, sql in statements if not owned]
    assert not unowned, f"Shared-connection SQL without Store ownership: {unowned[:3]}"


def test_concurrent_access_is_serialized(store, monkeypatch):
    import threading
    import time

    # Three rounds cover insert, replacement, then removal/reordering. Four
    # workers must actually contend before each round; scheduling luck and 200
    # repeated publications are not needed to exercise the shared connection.
    count, rounds = 4, 3
    iid = store.accounts.upsert_account("main", "cred", None, True)
    condition = threading.Condition()
    start, finish = (threading.Barrier(count + 1) for _ in range(2))
    phase = threading.Barrier(count)
    barriers = (start, finish, phase)
    blocked = [set() for _ in range(rounds)]
    reentered, statements, errors = set(), [], []
    round_number = 0
    real_acquire = store._lock.acquire

    def observe_acquire(blocking=True, timeout=-1):
        # Keep the original ServingLock, its reentrant ownership and the SQLite
        # lock. Only observe a failed real acquisition before its ordinary wait.
        if blocking and timeout == -1:
            if store._lock.held_by_current_thread:
                with condition:
                    reentered.add(threading.current_thread().name)
            if real_acquire(blocking=False):
                return True
            with condition:
                blocked[round_number].add(threading.current_thread().name)
                condition.notify_all()
        return real_acquire(blocking=blocking, timeout=timeout)

    def observe_sql(sql):
        with condition:
            statements.append((threading.current_thread().name,
                               store._lock.held_by_current_thread, sql))

    def worker(n):
        try:
            tids = []
            for i in range(rounds):
                start.wait(10)
                tids.append(store.tracks.upsert_track(
                    f"v{n}_{i}", f"Song {n} {i}", "Artist", "Alb", 200 + i))
                phase.wait(10)
                pid = store.playlists.upsert_playlist(
                    iid, f"PL{n}", f"Mix {n} {i}", min(i + 1, 2), f"h{n}_{i}", 1000 + i)
                phase.wait(10)
                store.playlists.set_playlist_tracks(pid, list(reversed(tids[-2:])))
                phase.wait(10)
                assert {p.provider_playlist_id for p in store.playlists.get_playlists()} == {
                    f"PL{other}" for other in range(count)}
                # This public method calls another repository read while it
                # still owns the Store lock, exercising actual re-entry.
                assert store.playlists.get_playlist_for_account(iid, f"PL{n}").id == pid
                finish.wait(10)
        except BaseException as error:
            with condition:
                errors.append((n, repr(error)))
                condition.notify_all()
            for barrier in barriers:
                barrier.abort()

    names = {f"store-worker-{n}" for n in range(count)}
    threads = [threading.Thread(target=worker, args=(n,), name=f"store-worker-{n}")
               for n in range(count)]
    started = []
    completed = False
    monkeypatch.setattr(store._lock, "acquire", observe_acquire)
    store.conn.set_trace_callback(observe_sql)
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
        for round_number in range(rounds):
            with store._lock:
                start.wait(10)
                with condition:
                    assert condition.wait_for(
                        lambda: blocked[round_number] == names or errors, timeout=10), blocked
                    assert not errors, errors
                # Every worker is inside its real repository's acquisition,
                # waiting for this owner. Release it before any phase barrier.
            finish.wait(10)
        completed = True
    finally:
        if not completed:
            for barrier in barriers:
                barrier.abort()
        deadline = time.monotonic() + 10
        for thread in started:
            thread.join(max(0, deadline - time.monotonic()))
        store.conn.set_trace_callback(None)
    assert not [thread.name for thread in started if thread.is_alive()]
    assert errors == []
    assert blocked == [names] * rounds
    assert reentered == names
    assert {name for name, _, _ in statements} == names
    _assert_shared_store_sql_owned(statements)

    playlists = store.playlists.get_playlists()
    assert {p.provider_playlist_id: (p.account_id, p.title, p.track_count,
                                   p.content_hash, p.first_seen, p.last_seen, p.last_changed)
            for p in playlists} == {
        f"PL{n}": (iid, f"Mix {n} 2", 2, f"h{n}_2", 1000, 1002, 1002)
        for n in range(count)}
    assert [tuple(row) for row in store.conn.execute(
        "SELECT video_id,title,artist,duration_s FROM tracks ORDER BY video_id")] == [
        (f"v{n}_{i}", f"Song {n} {i}", "Artist", 200 + i)
        for n in range(count) for i in range(rounds)]
    assert [tuple(row) for row in store.conn.execute(
        "SELECT p.provider_playlist_id,t.video_id,pt.position FROM playlist_tracks pt "
        "JOIN playlists p ON p.id=pt.playlist_id JOIN tracks t ON t.id=pt.track_id "
        "ORDER BY p.provider_playlist_id,pt.position")] == [
        (f"PL{n}", f"v{n}_{i}", position)
        for n in range(count) for position, i in enumerate((2, 1))]


def test_shared_store_ownership_check_rejects_unserialized_write(store):
    import threading
    import pytest

    statements = []
    store.conn.set_trace_callback(lambda sql: statements.append((
        threading.current_thread().name, store._lock.held_by_current_thread, sql)))
    try:
        # A real catalog row write with only the repository's serialization
        # decorator bypassed. SQLite's per-call lock and durability stay intact.
        repo = store.tracks.repository
        assert repo.create_recording.__wrapped__(repo, "unowned", "Song", "Artist", 0) > 0
    finally:
        store.conn.set_trace_callback(None)
    with pytest.raises(AssertionError, match="SQL without Store ownership"):
        _assert_shared_store_sql_owned(statements)


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
