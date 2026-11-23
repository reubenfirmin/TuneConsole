"""Publication follows consumed inputs, rather than all writes on a busy serving connection."""
import gc
from contextlib import closing
import sqlite3
import weakref

import pytest

from yt_playlist.core import catalog
from yt_playlist.core.db_revisions import Revisions, column_group_reads
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.repos.base import GENERATED_GROUP


@pytest.fixture(params=["memory", "file"])
def database(request):
    return request.getfixturevalue('store' if request.param == 'memory' else 'file_backed_store')


def test_unrelated_settings_and_analytics_do_not_invalidate_catalog_snapshot(database):
    store = database
    store.tracks.upsert_track("video", "Song", "Artist", None, None)
    store.settings.set_setting("model-input", "old")
    published = []
    with store.read_snapshot() as snapshot:
        assert snapshot.store.conn.execute("SELECT COUNT(*) FROM catalog_tracks WHERE id IN (SELECT id FROM track_sources WHERE library_origin IS NOT NULL)").fetchone()[0] == 1
        assert snapshot.store.settings.get_setting("model-input") == "old"
        store.settings.set_setting("sync-watermark", "new")
        store.rec_ledger.record_serving_policy("new", {}, 10)
        assert snapshot.publish(lambda: published.append(True))
    assert published == [True]


def test_candidate_details_ignore_radio_membership_and_guard_liked_music(database):
    store = database
    account = store.accounts.upsert_account('main', None, True)
    store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    details = lambda reader: reader.recommendations.repository.candidate_details(['video'])['video']

    with store.read_snapshot() as snapshot:
        assert not details(snapshot.store)['liked']
        with atomic_write(store):
            store.playlists.repository.publish_radio_destination(
                account, 'PLradio', 'On Air', GENERATED_GROUP, ['video'], 'hash', None, None, 1)
        assert snapshot.is_current()
        likes = store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 2)
        assert not snapshot.is_current()

    with store.read_snapshot() as snapshot:
        assert not details(snapshot.store)['liked']
        store.playlists.repository.set_liked_membership(likes, 'video', True)
        assert not snapshot.is_current()

    with store.read_snapshot() as snapshot:
        assert details(snapshot.store)['liked']
        store.playlists.repository.set_liked_membership(likes, 'video', False)
        assert not snapshot.is_current()

    store.playlists.remove_playlist(likes)
    with store.read_snapshot() as snapshot:
        assert not details(snapshot.store)['liked']
        replacement = store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 3)
        assert replacement != likes
        assert not snapshot.is_current()


def test_vibe_selection_publishes_across_an_on_air_queue_replacement(database):
    store = database
    account = store.accounts.upsert_account('main', None, True)
    store.tracks.upsert_track('first', 'First', 'Artist')
    second = store.tracks.upsert_track('second', 'Second', 'Artist')
    unsaved = store.tracks.upsert_track('unsaved', 'Unsaved', 'Artist', library=False)
    store.tracks.set_track_genre(second, 'House')
    store.tracks.set_track_year(second, '1992')
    key = store.tracks.identity_key_for_video('second')
    unsaved_key = store.tracks.identity_key_for_video('unsaved')
    likes = store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 1)
    playlists = store.playlists.repository
    with atomic_write(store):
        radio, _ = playlists.publish_radio_destination(
            account, 'PLradio', 'On Air', GENERATED_GROUP, ['first'], 'first', None, None, 1)

    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.rec_query.group_candidates([key])[key]['video_id'] == 'second'
        assert reader.rec_query.recipe_facet_keys()[0]
        assert reader.rec_query.genre_play_distribution() == {'House': 1}
        assert reader.rec_query.era_play_distribution() == {'1990': 1}
        assert reader.modes.years_for([key]) == {key: 1992}
        assert reader.rec_model.repository.genre_rows([key]) == [(key, 'House')]
        assert reader.rec_candidates.comfort_candidates(1000, keys=[key]) == []
        snapshot.release_reader()
        playlists.publish_confirmed(radio, 'On Air', ['second'], 'second', [second], 2, empty_digest='')
        assert snapshot.publish(lambda: None)
        playlists.set_liked_membership(likes, 'second', True)
        assert not snapshot.is_current()

    with store.read_snapshot() as snapshot:
        assert snapshot.store.rec_query.group_candidates([unsaved_key]) == {}
        favorites = snapshot.store.rec_candidates.comfort_candidates(1000, keys=[key])
        assert [row['key'] for row in favorites] == [key]
        snapshot.release_reader()
        playlists.publish_confirmed(radio, 'On Air', ['unsaved'], 'unsaved', [unsaved], 3, empty_digest='')
        assert not snapshot.is_current()


@pytest.mark.parametrize("mutation", ["input", "missing-input", "raw-input", "view-input"])
def test_mutations_to_consumed_inputs_reject_publication(database, mutation):
    store = database
    tid = store.tracks.upsert_track("video", "Song", "Artist", None, None)
    store.settings.set_setting("input", "old")
    with store.read_snapshot() as snapshot:
        if mutation == "view-input":
            snapshot.store.conn.execute("SELECT genre FROM catalog_tracks WHERE id IN (SELECT id FROM track_sources WHERE library_origin IS NOT NULL)").fetchall()
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


@pytest.mark.parametrize('function', ['json_each', 'json_tree'])
def test_json_expansion_guards_its_input_without_rejecting_unrelated_writes(database, function):
    database.settings.set_setting('json-input', '["first"]')
    with database.read_snapshot() as snapshot:
        payload = snapshot.store.settings.get_setting('json-input')
        assert [row[0] for row in snapshot.store.conn.execute(
            f"SELECT value FROM {function}(?) WHERE type='text'", (payload,))] == ['first']
        with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
            snapshot.store.conn.execute("UPDATE settings SET value='blocked' WHERE key='json-input'")
        database.settings.set_setting('unrelated', 'changed')
        assert snapshot.publish(lambda: None)
    with database.read_snapshot() as snapshot:
        payload = snapshot.store.settings.get_setting('json-input')
        snapshot.store.conn.execute(f'SELECT value FROM {function}(?)', (payload,)).fetchall()
        database.settings.set_setting('json-input', '["second"]')
        assert not snapshot.publish(lambda: pytest.fail('Published stale JSON input'))


@pytest.mark.parametrize('name', ['json_each', 'json_tree'])
def test_real_tables_named_like_json_functions_keep_their_revision_guards(database, name):
    with database.conn:
        database.conn.execute(f'CREATE TABLE {name}(value TEXT)')
        database.conn.execute(f'INSERT INTO {name} VALUES ("first")')
    Revisions.unbind(database.conn)
    database._snapshot_revisions = Revisions(database.conn)
    with database.read_snapshot() as snapshot:
        assert snapshot.store.conn.execute(f'SELECT value FROM {name}').fetchone()[0] == 'first'
        with database.conn:
            database.conn.execute(f'UPDATE {name} SET value="second"')
        assert not snapshot.publish(lambda: pytest.fail('Ignored an actual table'))


def test_interrupted_writer_reservation_releases_transaction_before_retry(database):
    store = database
    interrupted = False

    def cancel():
        nonlocal interrupted
        if store.conn.in_transaction and not interrupted:
            interrupted = True
            return 1
        return 0

    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting('input')
        snapshot.release_reader()
        try:
            store.conn.set_progress_handler(cancel, 1)
            with pytest.raises(sqlite3.OperationalError, match='interrupt'):
                snapshot.publish(lambda: pytest.fail('Published an interrupted reservation'))
            assert interrupted and not store.conn.in_transaction
        finally:
            store.conn.set_progress_handler(None, 0)
    store.settings.set_setting('after-cancellation', 'available')
    with store.read_snapshot() as retry:
        assert retry.publish(lambda: None)


def test_cached_setting_reads_each_track_their_own_keys(database):
    store = database
    with store.read_snapshot() as snapshot:
        snapshot.store.settings.get_setting("first")
        snapshot.store.settings.get_setting("second")
        store.settings.set_setting("second", "new")
        assert not snapshot.publish(lambda: pytest.fail("missed second read dependency"))


def test_unannotated_setting_read_after_key_lookup_tracks_entire_table(database):
    store = database
    with store.read_snapshot(allow_stale_keys={('settings', 'key:third')}) as snapshot:
        snapshot.store.settings.get_setting("first")
        snapshot.store.conn.execute("SELECT value FROM settings WHERE key=?", ("second",)).fetchone()
        store.settings.set_setting("third", "new")
        assert not snapshot.publish(lambda: pytest.fail("missed raw setting read"))


def test_external_commit_and_schema_change_remain_conservative(file_backed_store):
    store = file_backed_store
    with store.read_snapshot() as snapshot:
        with closing(sqlite3.connect(store._db_path)) as other, other:
            other.execute("INSERT INTO settings VALUES ('external','changed')")
        assert not snapshot.publish(lambda: pytest.fail("missed external commit"))
    with store.read_snapshot() as snapshot:
        with closing(sqlite3.connect(store._db_path)) as other, other:
            other.execute("CREATE TABLE extra_input(value TEXT)")
        assert not snapshot.publish(lambda: pytest.fail("missed external commit"))


@pytest.mark.parametrize('change', ['new-owner', 'new-column'])
def test_new_schema_definitions_bind_counters_at_admission(database, change):
    store = database
    with store.conn:
        store.conn.execute('CREATE TABLE published_input(id INTEGER PRIMARY KEY,value TEXT)')
        store.conn.execute("INSERT INTO published_input VALUES (1,'initial')")
    if change == 'new-column':
        store.conn.execute('ALTER TABLE published_input ADD COLUMN fresh TEXT')
    Revisions.unbind(store.conn)
    store._snapshot_revisions = Revisions(store.conn)
    column = 'fresh' if change == 'new-column' else 'value'
    with store.read_snapshot() as snapshot:
        snapshot.store.conn.execute(f'SELECT {column} FROM published_input').fetchone()
        store.settings.set_setting('unrelated-input', 'changed')
        assert snapshot.publish(lambda: None)
    with store.read_snapshot() as snapshot:
        snapshot.store.conn.execute(f'SELECT {column} FROM published_input').fetchone()
        with store.conn:
            store.conn.execute(f'UPDATE published_input SET {column}=?', ('changed',))
        assert not snapshot.publish(lambda: pytest.fail('Missed a write to a newly published field'))


@pytest.mark.parametrize('change', ['timestamps', 'genre', 'insert', 'delete', 'rollback'])
def test_declared_catalog_read_guards_data_but_not_enrichment_schedule(database, change):
    store = database
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
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
            catalog.choose_field(store.conn, tid, 'genre', 'House')
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
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    with store.read_snapshot() as snapshot:
        snapshot.store.rec_query.track_content()
        snapshot.store.settings.get_setting('home_mix_vibe')
        with store.conn:
            catalog.choose_field(store.conn, tid, 'genre', 'House')
        store.settings.set_setting('home_mix_vibe', 'energetic')
        assert not snapshot.publish(lambda: pytest.fail('published superseded preferences'),
                                    allow_stale_tables={'recordings'})


def test_ranking_tolerance_keeps_external_writer_guard(file_backed_store):
    store = file_backed_store
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    with store.read_snapshot() as snapshot:
        snapshot.store.rec_query.track_content()
        with closing(sqlite3.connect(store._db_path)) as other, other:
            other.row_factory = sqlite3.Row
            catalog.choose_field(other, tid, 'genre', 'House')
        assert not snapshot.publish(lambda: pytest.fail('ignored an external writer'),
                                    allow_stale_tables={'recordings'})


def test_scoped_capture_keeps_complete_counters_and_bounds_work_after_key_churn(database):
    store = database
    store.tracks.upsert_track('video', 'Song', 'Artist', None, None)
    store.settings.set_setting('listening_timezone', 'UTC')
    wanted = {('recordings', ''), ('settings', 'key:listening_timezone'),
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
    changes = database.conn.total_changes
    assert snapshot.is_current() == (not changed)
    assert database.conn.total_changes == changes and not database.conn.in_transaction
    published = []
    assert snapshot.publish(lambda: published.append(True)) == (not changed)
    assert published == ([] if changed else [True])
