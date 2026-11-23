"""Whole-library auditing is explicit and separate from startup."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core.migrations import schema_definition
from yt_playlist.core.migrations import MigrationError, migrate
from yt_playlist.core.migrations import current
from yt_playlist.core.migrations.__main__ import main
from yt_playlist.core.migrations.audit import audit
from yt_playlist.core.store import Store


@pytest.fixture
def installed(tmp_path, consumer_store):
    path = tmp_path / 'installed.db'
    consumer_store(path)
    return path


def test_current_startup_keeps_writes_available_without_an_audit(installed, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Current startup invoked a full-library data audit')

    monkeypatch.setattr(schema_definition, '_validate_data', forbidden)
    store = Store(installed)
    try:
        store.init_schema()
        assert not store.activity_paused
        store.tracks.upsert_track('continuing-write', 'New song', 'Artist', None, 213)
        assert store.tracks.duration_for_video('continuing-write') == 213
    finally:
        store.conn.close()


def test_audit_reports_integrity_failure_without_repeating_or_advancing_migrations(installed):
    with closing(sqlite3.connect(installed)) as conn, conn:
        conn.execute("INSERT INTO track_sources(video_id,recording_id) VALUES ('dangling',999999)")
    with closing(sqlite3.connect(installed)) as conn, conn:
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
    with closing(sqlite3.connect(installed)) as conn, conn:
        conn.execute("INSERT INTO settings VALUES ('uncommitted','retain')")
        with pytest.raises(MigrationError, match='outstanding caller transaction'):
            audit(conn)
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM settings WHERE key='uncommitted'").fetchone() == ('retain',)
        conn.rollback()


def test_audit_cli_is_read_only_and_does_not_create_missing_databases(installed, tmp_path, capsys):
    with closing(sqlite3.connect(installed)) as conn:
        before = list(conn.iterdump())
    assert main(['audit', str(installed)]) == 0
    assert f'Integrity audit passed at schema {current.VERSION}' in capsys.readouterr().out
    with closing(sqlite3.connect(installed)) as conn:
        assert list(conn.iterdump()) == before
    missing = tmp_path / 'missing.db'
    with pytest.raises(SystemExit) as error:
        main(['audit', str(missing)])
    assert error.value.code == 1
    assert not missing.exists()
