"""Publication follows consumed inputs, rather than all writes on a busy serving connection."""
import gc
import sqlite3
import weakref

import pytest

from yt_playlist.core.store import Store
from yt_playlist.core.db_revisions import column_group_reads


@pytest.fixture(params=["memory", "file"])
def database(request, tmp_path):
    store = Store(":memory:" if request.param == "memory" else tmp_path / "state.db")
    store.init_schema()
    yield store
    store.conn.close()


def test_unrelated_settings_and_analytics_do_not_invalidate_catalog_snapshot(database):
    store = database
    store.tracks.upsert_track("video", "Song", "Artist", None, None)
    store.settings.set_setting("model-input", "old")
    published = []
    with store.read_snapshot() as snapshot:
        assert snapshot.store.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
        assert snapshot.store.settings.get_setting("model-input") == "old"
        store.settings.set_setting("sync-watermark", "new")
        store.rec_ledger.record_serving_policy("new", "{}", 10)
        assert snapshot.publish(lambda: published.append(True))
    assert published == [True]


@pytest.mark.parametrize("mutation", ["input", "missing-input", "raw-input", "view-input"])
def test_mutations_to_consumed_inputs_reject_publication(database, mutation):
    store = database
    tid = store.tracks.upsert_track("video", "Song", "Artist", None, None)
    store.settings.set_setting("input", "old")
    with store.read_snapshot() as snapshot:
        if mutation == "view-input":
            snapshot.store.conn.execute("SELECT genre FROM tracks").fetchall()
            store.tracks.set_track_genre(tid, "House")
        elif mutation == "raw-input":
            snapshot.store.conn.execute("SELECT * FROM settings").fetchall()
            store.settings.set_setting("other", "new")
        else:
            key = "input" if mutation == "input" else "not-yet-present"
            snapshot.store.settings.get_setting(key)
            store.settings.set_setting(key, "new")
        assert not snapshot.publish(lambda: pytest.fail("published stale inputs"))


def test_rolled_back_and_identical_updates_do_not_invalidate_snapshot(database):
    store = database
    store.settings.set_setting("input", "old")
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting("input") == "old"
        store.settings.set_setting("input", "old")
        store.conn.execute("UPDATE settings SET value='new' WHERE key='input'")
        store.conn.rollback()
        assert snapshot.publish(lambda: None)


def test_cached_setting_reads_each_track_their_own_keys(database):
    store = database
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting("first")
        snapshot.store.settings.get_setting("second")
        store.settings.set_setting("second", "new")
        assert not snapshot.publish(lambda: pytest.fail("missed second read dependency"))


def test_unannotated_setting_read_after_key_lookup_tracks_entire_table(database):
    store = database
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting("first")
        snapshot.store.conn.execute("SELECT value FROM settings WHERE key=?", ("second",)).fetchone()
        store.settings.set_setting("third", "new")
        assert not snapshot.publish(lambda: pytest.fail("missed raw setting read"))


def test_external_commit_and_schema_change_remain_conservative(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.init_schema()
    try:
        with store.read_snapshot() as snapshot:
            with sqlite3.connect(path) as other:
                other.execute("INSERT INTO settings VALUES ('external','changed')")
            assert not snapshot.publish(lambda: pytest.fail("missed external commit"))
        with store.read_snapshot() as snapshot:
            store.conn.execute("CREATE TABLE extra_input(value TEXT)")
            assert not snapshot.publish(lambda: pytest.fail("missed schema change"))
    finally:
        store.conn.close()


@pytest.mark.parametrize('change', ['timestamps', 'genre', 'insert', 'delete', 'rollback'])
def test_declared_catalog_read_guards_data_but_not_enrichment_schedule(database, change):
    store = database
    store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    if change == 'delete':
        with store.conn:
            store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('orphan')")
    with store.read_snapshot() as snapshot:
        with column_group_reads(snapshot.store, 'recordings', 'catalog'):
            snapshot.store.rec_query.track_content()
        if change == 'insert':
            store.tracks.upsert_track('new', 'Another', 'Artist', None, None)
        elif change == 'delete':
            with store.conn:
                store.conn.execute("DELETE FROM recordings WHERE identity_key='orphan'")
        elif change in ('genre', 'rollback'):
            store.conn.execute("UPDATE recordings SET genre='House'")
            if change == 'rollback':
                store.conn.rollback()
            else:
                store.conn.commit()
        else:
            with store.conn:
                store.conn.execute('UPDATE recordings SET first_enriched_at=1,last_enriched_at=2')
        assert snapshot.publish(lambda: None) is (change in ('timestamps', 'rollback'))


def test_catalog_scope_does_not_weaken_unannotated_reads(database):
    store = database
    store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    with store.read_snapshot() as snapshot:
        with column_group_reads(snapshot.store, 'recordings', 'catalog'):
            snapshot.store.rec_query.track_content()
        snapshot.store.conn.execute('SELECT last_enriched_at FROM recordings').fetchall()
        with store.conn:
            store.conn.execute('UPDATE recordings SET last_enriched_at=2')
        assert not snapshot.publish(lambda: None)


def test_ranking_tolerance_does_not_relax_unlisted_preferences(database):
    store = database
    store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    with store.read_snapshot() as snapshot:
        snapshot.store.rec_query.track_content()
        snapshot.store.settings.get_setting('home_mix_vibe')
        with store.conn:
            store.conn.execute("UPDATE recordings SET genre='House'")
        store.settings.set_setting('home_mix_vibe', 'energetic')
        assert not snapshot.publish(lambda: pytest.fail('published superseded preferences'),
                                    allow_stale_tables={'recordings'})


def test_ranking_tolerance_keeps_external_writer_guard(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    store.init_schema()
    store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    try:
        with store.read_snapshot() as snapshot:
            snapshot.store.rec_query.track_content()
            with sqlite3.connect(path) as other:
                other.execute("UPDATE recordings SET genre='House'")
            assert not snapshot.publish(lambda: pytest.fail('ignored an external writer'),
                                        allow_stale_tables={'recordings'})
    finally:
        store.conn.close()


def test_scoped_capture_keeps_complete_counters_and_bounds_work_after_key_churn(database):
    store = database
    store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    store.settings.set_setting('takeout_imported_at', '123')
    wanted = {('recordings', ''), ('settings', 'key:takeout_imported_at'),
              ('settings', 'key:missing')}

    def measured_capture():
        steps = 0

        def step():
            nonlocal steps
            steps += 1
            return 0

        store.conn.set_progress_handler(step, 1)
        try:
            result = store._snapshot_revisions.capture(wanted)
        finally:
            store.conn.set_progress_handler(None, 0)
        return result, steps

    before, small_steps = measured_capture()
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',
                               [(f'unrelated-{i}', 'value') for i in range(4000)])
        store.conn.execute("DELETE FROM settings WHERE key LIKE 'unrelated-%'")
    after, large_steps = measured_capture()
    assert after == before
    assert set(after) == wanted - {('settings', 'key:missing')}
    # Count VM instructions rather than timing a small query on a loaded test machine.
    assert large_steps <= small_steps + 100
    complete = store._snapshot_revisions.capture()
    assert len(complete) > 4000
    assert ('settings', 'key:unrelated-3999') in complete
    assert store._snapshot_revisions.capture(set()) == {}


def test_publication_seeks_only_consumed_revisions(database, monkeypatch):
    revisions = database._snapshot_revisions
    original, captures = revisions.capture, []
    def capture(dependencies=None):
        captures.append(dependencies)
        return original(dependencies)
    monkeypatch.setattr(revisions, 'capture', capture)
    with database.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting('input')
        database.settings.set_setting('unrelated', 'changed')
        assert snapshot.publish(lambda: None)
    assert captures == [{('settings', 'key:input')}]


@pytest.mark.parametrize('changed', [False, True])
def test_declared_snapshot_guards_missing_inputs_without_capturing_other_keys(database, changed):
    dependencies = {('settings', 'key:input')}
    with database.read_snapshot(dependencies=dependencies) as snapshot:
        assert snapshot.store.settings.get_setting('input') is None
        database.settings.set_setting('input' if changed else 'unrelated', 'new')
        assert snapshot.publish(lambda: None) is not changed


def test_declared_snapshot_rejects_undeclared_reads_and_does_not_reuse_memo(database):
    # Warm the ordinary memo while a setting has no revision counter yet.
    with database.read_snapshot() as snapshot:
        assert snapshot.store.memoized_read('probe', lambda: snapshot.store.settings.get_setting('input')) is None
    database.settings.set_setting('input', 'new')
    with database.read_snapshot(dependencies=set()) as snapshot:
        assert snapshot.store.memoized_read('probe', lambda: snapshot.store.settings.get_setting('input')) == 'new'
        with pytest.raises(RuntimeError, match='outside its declared revision scope'):
            snapshot.publish(lambda: pytest.fail('published an unguarded input'))
    assert not database.conn.in_transaction


def test_revision_capture_batches_below_sqlite_parameter_limit(database):
    with database.conn:
        database.conn.executemany('INSERT INTO settings VALUES (?,?)',
                                  [(f'input-{i}', 'value') for i in range(1200)])
    dependencies = {('settings', f'key:input-{i}') for i in range(1200)}
    previous = database.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    try:
        assert set(database._snapshot_revisions.capture(dependencies)) == dependencies
    finally:
        database.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


def test_closed_snapshot_releases_memo_callback_without_waiting_for_cycle_collection(database):
    collecting = gc.isenabled()
    gc.disable()
    try:
        with database.read_snapshot() as snapshot:
            snapshot.store.settings.get_setting('input')
            callback = weakref.ref(snapshot.store._memoized_read)
        del snapshot
        assert callback() is None
    finally:
        if collecting:
            gc.enable()


@pytest.mark.parametrize('changed', [False, True])
def test_released_reader_keeps_original_guard_after_context_exit(database, changed):
    database.settings.set_setting('input', 'old')
    with database.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('input') == 'old'
        snapshot.release_reader()
        snapshot.release_reader()
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            snapshot.store.conn.execute('SELECT 1')
    database.settings.set_setting('input' if changed else 'foreground-setting', 'new')
    published = []
    assert snapshot.publish(lambda: published.append(True)) == (not changed)
    assert published == ([] if changed else [True])
