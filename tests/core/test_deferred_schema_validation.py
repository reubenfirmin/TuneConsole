"""Readable predecessor admission; full-library audits are explicitly requested."""
from contextlib import ExitStack, closing
import sqlite3
import threading
import time

import pytest

from tests.conftest import seed_catalog_track
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, install_numbered_input
from yt_playlist.core import schema_definition as current
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.migrations.audit import audit
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Admission and audit tests do not construct the web application."""


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
        _pristine_installed_schema(79).backup(store.conn)
    return store._db_path


def test_deferred_startup_validates_structure_without_scanning_all_foreign_keys(predecessor):
    store = Store(predecessor)
    statements = []
    try:
        store.conn.set_trace_callback(statements.append)
        store.init_schema(defer_metadata=True)
        assert store._schema_compatibility and not store.activity_paused
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
        assert store._schema_compatibility and not store.activity_paused
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
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


@pytest.mark.parametrize('version', [79, 80, 81, 82, 85, 93, 103, 106, current.VERSION])
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


@pytest.mark.parametrize('version', [80, 81, 82, current.VERSION])
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


@pytest.mark.parametrize('version', [81, 82, current.VERSION])
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


@pytest.mark.parametrize('version', [81, current.VERSION])
def test_app_loads_saved_genres_without_writes_or_waiting_for_bulk_conversion(installed_library, version, request):
    from yt_playlist.providers import genres
    from yt_playlist.util import genre_catalog
    from yt_playlist.web.app import create_app

    path = installed_library(version)
    with closing(sqlite3.connect(path)) as setup, setup:
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


def test_installed_v84_regular_ddl_retains_writable_owners_and_snapshot_guards(
        tmp_path, caplog, installed_library):
    """An independent schema writer never starts the frozen bulk conversions."""
    import hashlib
    import json
    import logging
    from pathlib import Path
    import sys
    import traceback

    from yt_playlist.core import db_revisions, db_snapshot, storage_maintenance
    from yt_playlist.core.migrations import v085_genre_preferences, v086_genre_classifications
    from yt_playlist.providers import genres

    caplog.set_level(logging.INFO, logger='yt_playlist.core.migrations.runner')
    modules = (current, registry, runner, db_revisions, db_snapshot,
               storage_maintenance, sys.modules[Store.__module__],
               v085_genre_preferences, v086_genre_classifications)
    sources = {module.__name__: Path(module.__file__).resolve() for module in modules}
    sources[__name__] = Path(__file__).resolve()

    def fingerprints():
        return {name: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                for name, path in sources.items()}

    report = {'status': 'failed', 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'installed_version': 84, 'target_version': current.VERSION,
              'completion_budget_seconds': 15,
              'sources_before': fingerprints()}
    path = installed_library(84)
    setup = sqlite3.connect(path)
    try:
        with setup:
            setup.execute("INSERT INTO genre_whitelist(name) VALUES ('Jazz')")
            setup.execute("INSERT INTO settings VALUES ('upgrade-preserved','original')")
    finally:
        setup.close()
    store = Store(path)
    resumed = []
    maintenance = StorageMaintenance(store, on_resume=lambda: resumed.append(True))
    try:
        store.init_schema(defer_metadata=True)
        report['initial'] = {
            'pending': store._schema_recovery_pending, 'paused': store.activity_paused,
            'version': store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],
        }
        assert report['initial'] == {'pending': False, 'paused': False, 'version': 84}
        assert store._schema_compatibility
        history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        original_revisions = store._snapshot_revisions
        assert 'genre_whitelist' in original_revisions.tables
        with store.read_snapshot() as stale:
            assert stale.store.settings.get_setting('upgrade-preserved') == 'original'
            assert stale.store.genres.get_genre_whitelist() == ['Jazz']
            stale.release_reader()  # Retain the guard without pinning the predecessor WAL view.

        started = time.monotonic()
        maintenance.start()
        # Ready only waits for bounded regular DDL. It never waits for a
        # conversion, owner replacement, repository rebind or full data audit.
        ready = maintenance.ready.wait(report['completion_budget_seconds'])
        report['completion_signal'] = ready
        if not ready:
            report['phase_at_timeout'] = maintenance.phase
            frame = sys._current_frames().get(maintenance._thread.ident)
            report['maintenance_stack_at_timeout'] = traceback.format_stack(frame) if frame else []
        assert ready, f'Cutover did not finish: {maintenance.phase}'
        maintenance.join(1)
        report['cutover_seconds'] = time.monotonic() - started
        report['final'] = {
            'phase': maintenance.phase, 'pending': store._schema_recovery_pending,
            'paused': store.activity_paused, 'active': maintenance.active,
            'version': store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],
            'resume_calls': len(resumed),
        }
        assert report['final'] == {
            'phase': 'done', 'pending': False, 'paused': False, 'active': False,
            'version': 84, 'resume_calls': 0,
        }
        assert store.tracks.duration_for_video('committed') == 213
        assert store.settings.get_setting('upgrade-preserved') == 'original'
        assert 'Jazz' in store.genres.get_genre_whitelist()
        assert store._snapshot_revisions is original_revisions
        assert 'genre_whitelist' in store._snapshot_revisions.tables
        assert 'genre_preferences' not in store._snapshot_revisions.tables
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == history
        assert store.conn.execute('SELECT version FROM schema_changes').fetchone()[0] == 1
        assert store.conn.execute('SELECT * FROM independent_preparation_ddl').fetchall() == []
        report['installed_owners_retained'] = True
        # The schema guard conservatively rejects work captured before any DDL.
        # It must not publish against a schema other than the one it observed.
        assert not stale.publish(lambda: pytest.fail('Changed schema bypassed the guard'))
        report['stale_snapshot_rejected'] = True

        # Original counters must still reject changed consumed inputs.
        with store.read_snapshot() as fresh:
            assert fresh.store.settings.get_setting('after-upgrade') is None
            fresh.release_reader()
        store.settings.set_setting('after-upgrade', 'writable')
        assert store.settings.get_setting('after-upgrade') == 'writable'
        assert not fresh.publish(lambda: pytest.fail('Changed settings bypassed rebound counters'))
        with store.read_snapshot() as unchanged:
            assert unchanged.store.settings.get_setting('after-upgrade') == 'writable'
            unchanged.release_reader()
        assert unchanged.publish(lambda: store.settings.publish_values({'published-after-upgrade': 'yes'}))
        assert store.settings.get_setting('published-after-upgrade') == 'yes'
        report['post_cutover_writes_and_guards'] = True
        assert fingerprints() == report['sources_before']
        report['status'] = 'passed'
    finally:
        maintenance.stop()
        try:
            maintenance.join(5)
        finally:
            report['maintenance_phase_at_cleanup'] = maintenance.phase
            report['maintenance_log'] = [
                {'logger': record.name, 'level': record.levelname, 'message': record.getMessage(),
                 'exception': ''.join(traceback.format_exception(*record.exc_info)) if record.exc_info else None}
                for record in caplog.records
                if record.name in ('yt_playlist.core.migrations.runner',
                                   'yt_playlist.core.storage_maintenance')
            ][-30:]
            report['sources_after'] = fingerprints()
            if maintenance._thread is None or not maintenance._thread.is_alive():
                try:
                    report['state_at_cleanup'] = {
                        'version': store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],
                        'pending': store._schema_recovery_pending, 'paused': store.activity_paused,
                        'resume_calls': len(resumed),
                    }
                except sqlite3.Error as error:
                    report['cleanup_state_error'] = str(error)
                store.conn.close()
            (tmp_path / 'deferred-v84-cutover.json').write_text(json.dumps(report, indent=2) + '\n')
            genres.set_active(None)
