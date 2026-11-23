"""Current-schema admission and explicit full-library audits."""
from contextlib import closing
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tests.conftest import seed_catalog_track
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core import recording_selection
from yt_playlist.core.migrations import schema_definition as current
from yt_playlist.core.migrations import runner
from yt_playlist.core.migrations.audit import audit
from yt_playlist.core.store import Store
from yt_playlist.rec.rec_worker import RecWorker


@pytest.fixture(autouse=True)
def disable_periodic_library_sync(monkeypatch):
    """Admission consumes a saved Jazz vocabulary, without the web lifecycle."""
    bound_catalog(monkeypatch, entries=(('jazz', 'Jazz'),))


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'),)) as schema:
        yield schema


@pytest.fixture
def installed_library(tmp_path, _pristine_current_schema):
    path = tmp_path / 'installed.db'
    setup = Store(path)
    with closing(setup.conn):
        setup.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(setup, _pristine_current_schema)
        seed_catalog_track(setup, 'committed song|artist', 'Committed song', 'Artist', video_id='committed')
        with setup.conn:
            from yt_playlist.core import track_sources
            entry = setup.conn.execute("SELECT id FROM track_sources WHERE video_id='committed'").fetchone()[0]
            track_sources.write_field(setup.conn, entry, 'duration_s', 213)
    return path


def test_current_schema_admits_without_data_scans_or_writer_reservation(installed_library, monkeypatch):
    path = installed_library
    store = Store(path)
    with closing(store.conn), closing(sqlite3.connect(path)) as writer:
        with writer:
            writer.execute('UPDATE recordings SET effective_duration_s=NULL,has_playable_source=0')
        statements = []
        try:
            writer.execute('BEGIN IMMEDIATE')
            store.conn.set_trace_callback(statements.append)

            def no_full_scan(*args, **kwargs):
                pytest.fail('Foreground admission called the whole-data validator')

            def no_foreign_key_scan(action, first, second, database, source):
                if action == sqlite3.SQLITE_PRAGMA and first.lower() == 'foreign_key_check':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            store.conn.set_authorizer(no_foreign_key_scan)
            with monkeypatch.context() as foreground:
                foreground.setattr(current, '_validate_data', no_full_scan)
                started = time.monotonic()
                store.init_schema()
                with store.read_view() as reader:
                    assert reader.tracks.duration_for_video('committed') == 213
                    assert reader.tracks.identity_key_for_video('committed')
                assert time.monotonic() - started < 2
            assert not store.activity_paused
            assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == current.VERSION
            assert not any(sql.upper().startswith('BEGIN IMMEDIATE') for sql in statements)
            assert not any(sql.upper().startswith('PRAGMA FOREIGN_KEY_CHECK') for sql in statements)
            assert not recording_selection.ready(store.conn)
            statements.clear()
            worker = RecWorker(SimpleNamespace(store=store, now_fn=time.time, logger=logging.getLogger(__name__)))
            worker._poll_mix_selection()
            assert not recording_selection.ready(store.conn)
        finally:
            writer.rollback()
            try:
                store.conn.set_authorizer(None)
            except sqlite3.ProgrammingError:
                pass  # Admission failure already closes its connection.
        worker._poll_mix_selection()
        assert recording_selection.ready(store.conn)
        assert tuple(store.conn.execute('SELECT effective_duration_s,has_playable_source FROM recordings '
                                       "WHERE identity_key='committed song|artist'").fetchone()) == (213, 1)
        assert not any('sqlite_schema' in sql.lower() for sql in statements)


def test_explicit_audit_reports_bad_data_separately_from_admission(installed_library):
    path = installed_library
    with closing(sqlite3.connect(path)) as corrupt, corrupt:
        corrupt.execute("INSERT INTO track_sources(video_id,recording_id) VALUES ('dangling',999999)")
    store = Store(path)
    try:
        store.init_schema()
        before = list(store.conn.iterdump())
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader, reader:
            reader.row_factory = sqlite3.Row
            with pytest.raises(runner.MigrationError, match='foreign-key'):
                audit(reader)
        assert list(store.conn.iterdump()) == before
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == current.VERSION
    finally:
        store.conn.close()


def test_explicit_audit_uses_private_snapshot_without_gating_admission(installed_library, monkeypatch):
    path = installed_library
    store = Store(path)
    entered, release = threading.Event(), threading.Event()
    original = current._validate_data
    scanned = []

    def held_validation(conn, version, *, compact_artists=False):
        scanned.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(conn, version, compact_artists=compact_artists)

    monkeypatch.setattr(current, '_validate_data', held_validation)
    errors, results = [], []
    def inspect():
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader, reader:
                reader.row_factory = sqlite3.Row
                results.append(audit(reader))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=inspect)
    try:
        store.init_schema()
        assert not scanned
        thread.start()
        assert entered.wait(5)
        started = time.monotonic()
        with store.read_view() as reader:
            assert reader.tracks.duration_for_video('committed') == 213
        assert time.monotonic() - started < 2
        # The write must commit while the full audit pins its read snapshot.
        store.settings.set_setting('during-explicit-audit', 'writable')
        assert not release.is_set()
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert not errors
        assert [result.version for result in results] == [current.VERSION]
        assert scanned and all(owner != threading.get_ident() for owner in scanned)
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == current.VERSION
        assert store.settings.get_setting('during-explicit-audit') == 'writable'
    finally:
        release.set()
        if thread.ident is not None:
            thread.join()
        store.conn.close()


def test_audit_interruption_preserves_writes_history_and_revisions(installed_library, monkeypatch):
    path = installed_library
    store = Store(path)
    entered, stop = threading.Event(), threading.Event()
    errors = []
    original = current._validate_data

    def progress():
        entered.set()
        return int(stop.is_set())

    def slow_validation(conn, version, *, compact_artists=False):
        conn.set_progress_handler(progress, 1000)
        conn.execute('WITH RECURSIVE counter(n) AS (VALUES(0) UNION ALL '
                     'SELECT n+1 FROM counter) SELECT max(n) FROM counter').fetchone()

    def inspect():
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader, reader:
                reader.row_factory = sqlite3.Row
                audit(reader)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=inspect)
    try:
        store.init_schema()
        revisions = store._snapshot_revisions
        before = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        monkeypatch.setattr(current, '_validate_data', slow_validation)
        thread.start()
        assert entered.wait(5)
        store.settings.set_setting('during-cancelled-audit', 'retained')
        stop.set()
        thread.join(10)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], sqlite3.OperationalError)
        assert 'interrupt' in str(errors[0]).lower()
        assert not store.activity_paused
        assert store._snapshot_revisions is revisions
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == before
        assert store.tracks.duration_for_video('committed') == 213
        assert store.settings.get_setting('during-cancelled-audit') == 'retained'
        # Retrying is explicit and does not require restarting the application.
        monkeypatch.setattr(current, '_validate_data', original)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader, reader:
            reader.row_factory = sqlite3.Row
            assert audit(reader).version == current.VERSION
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join()
        store.conn.close()
