"""Readable predecessor admission; full-library audits are explicitly requested."""
from contextlib import ExitStack, closing
import sqlite3
import threading
import time

import pytest

from tests.conftest import seed_catalog_track
from tests.genre_catalog_support import bound_catalog
from tests.regular_schema_support import register_independent_ddl
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, install_numbered_input, without_query_timing,
)
from yt_playlist.core import schema_definition as current
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.migrations.audit import audit
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync(monkeypatch):
    """Admission consumes a saved Jazz vocabulary, without the web lifecycle."""
    bound_catalog(monkeypatch, entries=(('jazz', 'Jazz'),))


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'),)) as schema:
        yield schema


@pytest.fixture(scope='module')
def _pristine_installed_schema():
    # Admission and full audits remain real in each test. Their empty numbered
    # inputs do not need every earlier data conversion or the shipped vocabulary.
    schemas = {}
    with ExitStack() as resources:
        def get(version):
            if version not in schemas:
                conn = resources.enter_context(closing(sqlite3.connect(':memory:')))
                with pytest.MonkeyPatch.context() as patch:
                    bound_catalog(patch, entries=(('jazz', 'Jazz'),))
                    install_numbered_input(conn, version)
                conn.execute('PRAGMA query_only=ON')
                schemas[version] = conn
            return schemas[version]
        yield get


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'predecessor.db')
    with closing(store.conn):
        _pristine_installed_schema(107).backup(store.conn)
    return store._db_path


def test_deferred_startup_validates_structure_without_scanning_all_foreign_keys(predecessor):
    store = Store(predecessor)
    statements = []
    try:
        store.conn.set_trace_callback(statements.append)
        store.init_schema(defer_metadata=True)
        assert not store._schema_compatibility and not store.activity_paused
        assert not any(sql.strip().upper().startswith('PRAGMA FOREIGN_KEY_CHECK') for sql in statements)
        with closing(sqlite3.connect(predecessor.as_uri() + '?mode=ro', uri=True)) as reader, reader:
            reader.row_factory = sqlite3.Row
            statements.clear()
            reader.set_trace_callback(statements.append)
            audit(reader)
        assert sum(sql.strip().upper() == 'PRAGMA FOREIGN_KEY_CHECK' for sql in statements) == 1
    finally:
        store.conn.close()


def test_explicit_audit_rejects_broken_predecessor_without_running_conversion(predecessor):
    with closing(sqlite3.connect(predecessor)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('dangling',999999)")
    store = Store(predecessor)
    try:
        store.init_schema(defer_metadata=True)
        before = list(store.conn.iterdump())
        with closing(sqlite3.connect(predecessor.as_uri() + '?mode=ro', uri=True)) as reader, reader:
            reader.row_factory = sqlite3.Row
            with pytest.raises(runner.MigrationError, match='foreign-key'):
                audit(reader)
        assert list(store.conn.iterdump()) == before
        assert not store._schema_compatibility and not store.activity_paused
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 107
        assert store.conn.execute("SELECT recording_id FROM youtube_items WHERE video_id='dangling'").fetchone()[0] == 999999
    finally:
        store.conn.close()


def test_deferred_startup_still_rejects_missing_owner_structure(predecessor):
    with closing(sqlite3.connect(predecessor)) as conn, conn:
        conn.execute('DROP INDEX ix_group_library_inputs')
    store = Store(predecessor)
    try:
        with pytest.raises(runner.MigrationError, match='ix_group_library_inputs'):
            store.init_schema(defer_metadata=True)
    finally:
        store.conn.close()


@pytest.fixture
def installed_library(tmp_path, _pristine_current_schema, _pristine_installed_schema):
    """Copy only the empty prefix; every admission/audit uses its own file."""
    def build(version):
        path = tmp_path / 'installed.db'
        setup = Store(path)
        try:
            setup.conn.execute('PRAGMA synchronous=OFF')
            if version == current.VERSION:
                initialize_consumer(setup, _pristine_current_schema)
            else:
                _pristine_installed_schema(version).backup(setup.conn)
                setup._bind_repositories()
            seed_catalog_track(setup, 'committed song|artist', 'Committed song', 'Artist', video_id='committed')
            with setup.conn:
                setup.conn.execute("UPDATE youtube_items SET duration_s=213 WHERE video_id='committed'")
            setup.conn.execute('PRAGMA journal_mode=WAL')
        finally:
            setup.conn.close()
        return path

    return build


@pytest.mark.parametrize('version', [current.VERSION])
def test_every_readable_schema_admits_without_data_scans_or_writer_reservation(installed_library, monkeypatch, version):
    path = installed_library(version)
    store = Store(path)
    with closing(store.conn), closing(sqlite3.connect(path)) as writer:
        statements = []
        try:
            writer.execute('BEGIN IMMEDIATE')
            store.conn.set_trace_callback(statements.append)

            def no_full_scan(conn,version):
                pytest.fail('Foreground admission called the whole-data validator')

            def no_foreign_key_scan(action, first, second, database, source):
                if action == sqlite3.SQLITE_PRAGMA and first.lower() == 'foreign_key_check':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            store.conn.set_authorizer(no_foreign_key_scan)
            with monkeypatch.context() as foreground:
                foreground.setattr(current, '_validate_data', no_full_scan)
                started = time.monotonic()
                store.init_schema(defer_metadata=True)
                with store.read_view() as reader:
                    assert reader.tracks.duration_for_video('committed') == 213
                    assert reader.tracks.identity_key_for_video('committed')
                assert time.monotonic() - started < 2
            assert store._schema_compatibility == (version < current.VERSION)
            assert not store.activity_paused and not store._schema_recovery_pending
            assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
            assert not any(sql.upper().startswith('BEGIN IMMEDIATE') for sql in statements)
            assert not any(sql.upper().startswith('PRAGMA FOREIGN_KEY_CHECK') for sql in statements)
        finally:
            writer.rollback()
            try:
                store.conn.set_authorizer(None)
            except sqlite3.ProgrammingError:
                pass  # Admission failure already closes its connection.


@pytest.mark.parametrize('version', [current.VERSION])
def test_explicit_audit_reports_bad_data_separately_from_admission(installed_library, version):
    path = installed_library(version)
    with closing(sqlite3.connect(path)) as corrupt, corrupt:
        corrupt.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('dangling',999999)")
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        before = list(store.conn.iterdump())
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader, reader:
            reader.row_factory = sqlite3.Row
            with pytest.raises(runner.MigrationError, match='foreign-key'):
                audit(reader)
        assert list(store.conn.iterdump()) == before
        assert store._schema_compatibility == (version < current.VERSION)
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    finally:
        store.conn.close()


@pytest.mark.parametrize('version', [current.VERSION])
def test_explicit_audit_uses_private_snapshot_without_gating_admission(installed_library, monkeypatch, version):
    path = installed_library(version)
    store = Store(path)
    entered, release = threading.Event(), threading.Event()
    original = current._validate_data
    scanned = []

    def held_validation(conn,version):
        scanned.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(conn,version)

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
        store.init_schema(defer_metadata=True)
        assert not scanned
        thread.start()
        assert entered.wait(5)
        started = time.monotonic()
        with store.read_view() as reader:
            assert reader.tracks.duration_for_video('committed') == 213
        assert time.monotonic() - started < 2
        assert store._schema_compatibility == (version < current.VERSION)
        # This write uses an unchanged owner in both formats. It must commit
        # while the separate full audit still pins its earlier read snapshot.
        store.settings.set_setting('during-explicit-audit', 'writable')
        assert not release.is_set()
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert not errors
        assert [result.version for result in results] == [version]
        assert scanned and all(owner != threading.get_ident() for owner in scanned)
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
        assert store.settings.get_setting('during-explicit-audit') == 'writable'
    finally:
        release.set()
        if thread.ident is not None:
            thread.join()
        store.conn.close()


def test_audit_interruption_preserves_writes_history_and_revisions(installed_library, monkeypatch):
    path = installed_library(current.VERSION)
    store = Store(path)
    entered, stop = threading.Event(), threading.Event()
    errors = []
    original = current._validate_data

    def slow_validation(conn, version):
        conn.execute('WITH RECURSIVE counter(n) AS (VALUES(0) UNION ALL '
                     'SELECT n+1 FROM counter) SELECT max(n) FROM counter').fetchone()

    def inspect():
        def progress():
            entered.set()
            return int(stop.is_set())
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader, reader:
                reader.row_factory = sqlite3.Row
                reader.set_progress_handler(progress, 1000)
                audit(reader)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=inspect)
    try:
        store.init_schema(defer_metadata=True)
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
        assert not store.activity_paused and not store._schema_recovery_pending
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


@pytest.mark.parametrize('version', [current.VERSION])
@pytest.mark.usefixtures('without_query_timing')
def test_app_loads_saved_genres_without_writes_or_waiting_for_bulk_conversion(installed_library, version, monkeypatch):
    from fastapi import APIRouter
    from yt_playlist.providers import genres
    from yt_playlist.util import genre_catalog
    from yt_playlist.web import app as webapp
    from yt_playlist.web.app import create_app

    # This startup contract owns saved genre configuration and real maintenance,
    # independent of the route table. Keep create_app and its actual context.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    path = installed_library(version)
    with closing(sqlite3.connect(path)) as setup, setup:
        if version < current.VERSION:
            register_independent_ddl(monkeypatch, setup)
        setup.execute("INSERT OR REPLACE INTO settings VALUES ('genre_catalog_revision','previous')")
        if version < 85:
            setup.execute("INSERT INTO genre_whitelist(name) VALUES ('Jazz')")
        else:
            setup.execute("INSERT INTO genre_preferences VALUES ('jazz',1,'custom')")
    store = Store(path)
    with closing(store.conn), closing(sqlite3.connect(path)) as writer:
        maintenance = None
        try:
            writer.execute('BEGIN IMMEDIATE')
            store.init_schema(defer_metadata=True)
            started = time.monotonic()
            app = create_app(store, lambda: {}, background_work=False)
            assert time.monotonic() - started < 5
            assert store.settings.get_setting('genre_catalog_revision') == 'previous'
            assert genres.match_tag('jazz') == 'Jazz'
            assert store.genres.get_genre_whitelist() == ['Jazz']
            writer.rollback()
            maintenance = app.state.ctx.storage_maintenance
            maintenance.start()
            maintenance.join(15)
            if version < current.VERSION:
                assert store.conn.execute('SELECT * FROM independent_preparation_ddl').fetchall() == []
            assert not store.activity_paused and not store._schema_recovery_pending
            assert store.settings.get_setting('genre_catalog_revision') == 'previous'
            assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
            assert 'Jazz' in store.genres.get_genre_whitelist()
            assert genres.match_tag('jazz') == 'Jazz'
        finally:
            writer.rollback()
            if maintenance is not None:
                maintenance.stop()
                maintenance.join(timeout=None)
            genres.set_active(None)
