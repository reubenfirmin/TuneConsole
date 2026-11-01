"""Whole-library auditing is explicit and cannot gate an unrelated schema change."""
import sqlite3
import threading

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import current, registry, regular
from yt_playlist.core.migrations.__main__ import main
from yt_playlist.core.migrations.audit import audit
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.core.store import Store


@pytest.fixture
def installed(tmp_path):
    path = tmp_path / 'installed.db'
    conn = sqlite3.connect(path)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        migrate(conn)
    finally:
        conn.close()
    return path


def test_current_startup_keeps_writes_available_without_an_audit(installed, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Current startup invoked a full-library data audit')

    monkeypatch.setattr(current, '_validate_data', forbidden)
    store = Store(installed)
    maintenance = StorageMaintenance(store)
    try:
        store.init_schema(defer_metadata=True)
        assert not store._schema_recovery_pending
        assert not store.activity_paused
        maintenance.start()
        assert not maintenance.active
        assert maintenance.ready.is_set()
        store.tracks.upsert_track('continuing-write', 'New song', 'Artist', None, 213)
        assert store.tracks.duration_for_video('continuing-write') == 213
    finally:
        maintenance.stop()
        maintenance.join(5)
        store.conn.close()


def test_audit_reports_integrity_failure_without_repeating_or_advancing_migrations(installed):
    with sqlite3.connect(installed) as conn:
        conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('dangling',999999)")
    with sqlite3.connect(installed) as conn:
        before = list(conn.iterdump())
        statements = []
        conn.set_trace_callback(statements.append)
        # An unrelated corrupt row belongs to explicit auditing. Merely opening
        # an already-current schema must not repeat the full-library FK scan.
        assert migrate(conn) is False
        assert not any(sql.upper().startswith('PRAGMA FOREIGN_KEY_CHECK') for sql in statements)
        with pytest.raises(MigrationError, match='foreign-key'):
            audit(conn)
        assert not conn.in_transaction
        assert list(conn.iterdump()) == before


def test_audit_refuses_to_finish_or_rollback_a_callers_transaction(installed):
    with sqlite3.connect(installed) as conn:
        conn.execute("INSERT INTO settings VALUES ('uncommitted','retain')")
        with pytest.raises(MigrationError, match='outstanding caller transaction'):
            audit(conn)
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM settings WHERE key='uncommitted'").fetchone() == ('retain',)
        conn.rollback()


def test_suspended_audit_does_not_hold_up_independent_migration(installed, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    validate = current._validate_data

    def suspended(conn, version):
        entered.set()
        if not release.wait(10):
            raise AssertionError('Audit was not released')
        validate(conn, version)

    def inspect():
        try:
            with sqlite3.connect(installed.as_uri() + '?mode=ro', uri=True) as conn:
                conn.row_factory = sqlite3.Row
                results.append(audit(conn))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(current, '_validate_data', suspended)
    # A controlled additive successor exercises the production runner and exact
    # history, rather than calling its DDL outside the migration transaction.
    successor = regular.Change(len(regular.CHANGES) + 1, 'independent_addition',
                          lambda conn, ctx: conn.execute('CREATE TABLE independent_owner(id INTEGER PRIMARY KEY)'),
                          lambda conn: conn.execute('SELECT id FROM independent_owner LIMIT 0'))
    monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES + (successor,))
    thread = threading.Thread(target=inspect)
    thread.start()
    try:
        assert entered.wait(5)
        with sqlite3.connect(installed, timeout=.2) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            before = conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
            assert migrate(conn) is False
            assert conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall() == before
            assert conn.execute('SELECT name FROM schema_changes WHERE version=?',
                                (successor.version,)).fetchone() == (successor.name,)
            assert not release.is_set()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors
    assert [result.version for result in results] == [current.VERSION]


def test_audit_cli_is_read_only_and_does_not_create_missing_databases(installed, tmp_path, capsys):
    with sqlite3.connect(installed) as conn:
        before = list(conn.iterdump())
    assert main(['audit', str(installed)]) == 0
    assert f'Integrity audit passed at schema {current.VERSION}' in capsys.readouterr().out
    with sqlite3.connect(installed) as conn:
        assert list(conn.iterdump()) == before
    missing = tmp_path / 'missing.db'
    with pytest.raises(SystemExit) as error:
        main(['audit', str(missing)])
    assert error.value.code == 1
    assert not missing.exists()
