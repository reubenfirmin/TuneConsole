"""Real preparation, source mutations and recovery through genuine old layouts."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import numbered_store as installed, reuse_reporting_reader
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import listening_subjects as preparation


pytestmark = pytest.mark.parametrize('installed', [79, 99], indirect=True)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Listening preparation uses real SQL without constructing an HTTP app."""


def populated(store):
    # Completed catalog input is below listening preparation. Keep genuine
    # history/calendar rows and their SQL guards without metadata ingestion.
    seed_publication_subjects(store, [
        {'key': f'song {index}|artist', 'video_id': f'upload-{index}',
         'title': f'Song {index}', 'artist': 'Artist', 'duration_s': 120}
        for index in range(3)])
    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch):
        account = store.accounts.upsert_account('Main', 'main.json', None, True)
        for index in range(3):
            store.history.record_play_event(account, f'song {index}|artist', f'upload-{index}',
                1704844800 + index * 4000, source_timezone='UTC')
        store.history.record_history_plays(account, 1704844800,
            ['song 2|artist', 'unresolved|artist'], source_timezone='UTC')
    return account


def connect(store):
    conn = sqlite3.connect(store._db_path, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    # These private workers test SQL rollback, reopen and concurrent capture;
    # they do not simulate loss of the machine's filesystem write cache.
    conn.execute('PRAGMA synchronous=OFF')
    return closing(conn)


def finish(conn, *, batch_size=1):
    for _ in range(100):
        result = preparation.step(conn, batch_size=batch_size)
        if result.status == 'prepared':
            return result
    pytest.fail('Finite source preparation did not reach its current revision')


def assert_exact_sources(conn):
    for kind, source in preparation.SOURCES.items():
        expected = {row['id']:dict(row) for row in conn.execute('SELECT * FROM main.' + source)}
        actual = {row['id']:dict(row) for row in conn.execute('SELECT * FROM ' + preparation.DESTINATIONS[kind])}
        assert actual.keys() == expected.keys()
        for identity, row in expected.items():
            staged = actual[identity]
            assert json.loads(staged['source_snapshot']) == row
            assert staged['account_id'] == row['identity_id']
            assert staged['recording_id'] == row['recording_id']
            assert staged['unresolved_subject_key'] == (row['identity_key'] if row['recording_id'] is None else None)


def test_checkpoints_reconcile_changed_deleted_and_reused_ids_while_regular_ddl_advances(installed):
    store, _ = installed
    account = populated(store)
    with connect(store) as conn:
        preparation.install(conn)
        assert not conn.execute('SELECT 1 FROM prepared_listening_events').fetchone()
        first = preparation.step(conn, batch_size=1)
        assert first.processed == 1 and first.status == 'copying'
        cursor = first.cursor
    # Closing/reopening the independent job connection preserves its checkpoint.
    # No serving lock or open write transaction is retained between batches.
    store.settings.set_setting('during-listening-preparation', 'ordinary-write')
    regular.migrate(store.conn)
    assert store.conn.execute('SELECT version FROM schema_changes').fetchall()[0][0] == 1
    with store.conn:
        store.conn.execute("UPDATE main.play_events SET like_status='LIKE' WHERE id=?", (cursor,))
        store.conn.execute('DELETE FROM main.play_events WHERE id=3')
        # Reuse an ID behind the checkpoint. A cursor alone misses this input.
        store.conn.execute('DELETE FROM main.play_events WHERE id=?', (cursor,))
        store.conn.execute("INSERT INTO main.play_events(id,identity_id,identity_key,played_at,source) "
                           "VALUES (?,?,'replacement|artist',1704900000,'live')", (cursor, account))
        store.conn.execute("INSERT INTO main.play_events(id,identity_id,identity_key,played_at,source) "
                           "VALUES (-10,?,'negative-id|artist',1704900001,'takeout')", (account,))
    store.history.reset_play_history(account)
    store.history.record_history_plays(account, 1704931200, ['new-day|artist'], source_timezone='UTC')
    with connect(store) as conn:
        assert preparation.progress(conn).cursor == cursor
        assert conn.execute('SELECT count(*) FROM prepared_listening_events').fetchone()[0] == 1
        assert finish(conn).prepared_revision is not None
        assert_exact_sources(conn)
    assert store.settings.get_setting('during-listening-preparation') == 'ordinary-write'


def test_failed_batch_retains_prior_commit_and_requires_explicit_retry(installed):
    store, _ = installed
    account = populated(store)
    with connect(store) as conn:
        preparation.install(conn)
        first = preparation.step(conn, batch_size=1)
        original, other = [row[0] for row in conn.execute('SELECT recording_id FROM play_events ORDER BY id LIMIT 2')]
        with store.conn:
            store.conn.execute('UPDATE main.play_events SET recording_id=? WHERE id=2', (original,))
        with pytest.raises(MigrationError, match='disagrees with recording'):
            preparation.step(conn, batch_size=1)
        failed = preparation.progress(conn)
        assert failed.status == 'failed' and failed.cursor == first.cursor and failed.processed == first.processed
        assert conn.execute('SELECT count(*) FROM prepared_listening_events').fetchone()[0] == 1
        regular.migrate(store.conn)
        store.tracks.record_item_rating(account, 'upload-0', 'LIKE', source='api', now=1704900000)
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.step(conn, batch_size=1)
        assert preparation.progress(conn) == failed
        with store.conn:
            store.conn.execute('UPDATE main.play_events SET recording_id=? WHERE id=2', (other,))
        preparation.step(conn, batch_size=1, retry=True)
        finish(conn)
        assert_exact_sources(conn)
        assert store.tracks.retained_rating_statuses(account, ['upload-0']) == {'upload-0': 'LIKE'}


def test_interrupted_batch_rolls_back_only_itself_and_resumes_without_duplicates(installed, monkeypatch):
    store, _ = installed
    populated(store)
    with connect(store) as conn:
        preparation.install(conn)
        before = preparation.step(conn, batch_size=1)
        original = preparation._copy
        copied = []
        def interrupted(conn, kind, row):
            original(conn, kind, row)
            copied.append(row['id'])
            if len(copied) == 2:
                raise KeyboardInterrupt('interrupted before checkpoint commit')
        with monkeypatch.context() as patch:
            patch.setattr(preparation, '_copy', interrupted)
            with pytest.raises(KeyboardInterrupt, match='before checkpoint commit'):
                preparation.step(conn, batch_size=2)
        assert preparation.progress(conn) == before
        assert conn.execute('SELECT count(*) FROM prepared_listening_events').fetchone()[0] == 1
        finish(conn)
        assert_exact_sources(conn)


def test_prepared_revision_invalidates_on_new_writes_and_key_changes_without_foreground_scan(installed):
    store, _ = installed
    account = populated(store)
    with connect(store) as conn:
        preparation.install(conn)
        prepared = finish(conn)
        store.history.reset_play_history(account)
        invalidated = preparation.progress(conn)
        assert invalidated.status == 'reconciling' and invalidated.prepared_revision is None
        assert invalidated.processed == prepared.processed
        finish(conn)
        key, recording = conn.execute('SELECT identity_key,id FROM recordings ORDER BY id LIMIT 1').fetchone()
        before = preparation.progress(conn)
        # The final values are unchanged, but a real key edit crossed the
        # dependency boundary. Its live transaction only invalidates the proof.
        with store.conn:
            store.conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key + '-temporary', recording))
            store.conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key, recording))
        invalidated = preparation.progress(conn)
        assert invalidated.phase == 'events' and invalidated.cursor is None
        assert invalidated.processed == before.processed and invalidated.prepared_revision is None
        finish(conn)
        assert_exact_sources(conn)


def test_explicit_command_is_bounded_and_status_is_read_only(installed, tmp_path, capsys):
    store, _ = installed
    populated(store)
    assert preparation.main([str(store._db_path), '--batch-size', '1']) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['status'] == 'copying' and first['processed'] == 1
    before = list(store.conn.iterdump())
    assert preparation.main([str(store._db_path), '--status']) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert list(store.conn.iterdump()) == before
    assert preparation.main([str(store._db_path), '--batches', '20', '--batch-size', '1']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'prepared'
    missing = tmp_path / 'missing.db'
    with pytest.raises(SystemExit) as error:
        preparation.main([str(missing)])
    assert error.value.code == 1 and not missing.exists()


def test_dependency_rescan_includes_rows_added_after_the_initial_highwater(installed):
    store, _ = installed
    account = populated(store)
    with connect(store) as conn:
        preparation.install(conn)
        finish(conn)
        store.tracks.upsert_track('later', 'Later song', 'Another artist', None, 240)
        key = store.tracks.identity_key_for_video('later')
        store.history.record_play_event(account, key, 'later', 1705000000, source_timezone='UTC')
        finish(conn)
        recording = conn.execute("SELECT recording_id FROM youtube_items WHERE video_id='later'").fetchone()[0]
        with store.conn:
            store.conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key + '-mismatch', recording))
        with pytest.raises(MigrationError, match='disagrees with recording'):
            finish(conn)
        assert preparation.progress(conn).status == 'failed'
        with store.conn:
            store.conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (key, recording))
        preparation.step(conn, retry=True, batch_size=1)
        finish(conn)
        assert_exact_sources(conn)
