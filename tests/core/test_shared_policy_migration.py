"""The emergency upgrade is additive and never copies the bloated policy table."""
import sqlite3
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_space
from yt_playlist.core.migrations.runner import MigrationError, migrate


def test_current_schema_rejects_missing_shared_policy_structures(tmp_path, consumer_store):
    store = consumer_store(tmp_path / 'state.db')
    store.conn.execute('DROP INDEX ix_recommendation_policy_base')
    with pytest.raises(MigrationError, match='ix_recommendation_policy_base'):
        store.init_schema()


def test_migration_does_not_require_space_for_a_full_database_copy(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / 'state.db')
    conn.execute('CREATE TABLE original(id INTEGER)')
    conn.commit()
    monkeypatch.setattr(db_space.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    from yt_playlist.core.migrations import Migration
    migrate(conn, registry=[Migration(1, 'original', lambda c,x: c.execute('INSERT INTO original VALUES (1)'))])
    assert conn.execute('SELECT * FROM original').fetchall() == [(1,)]
    assert not (tmp_path / 'backups').exists()
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    conn.close()


def test_io_failure_diagnostic_names_database_and_available_space(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / 'state.db')
    error = sqlite3.OperationalError('disk I/O error')
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR
    monkeypatch.setattr(db_space.shutil, 'disk_usage', lambda path: SimpleNamespace(free=4096))
    hint = db_space.failure_hint(conn, error)
    assert str(tmp_path / 'state.db') in hint
    assert '0.0 MiB' in hint and 'preserve' in hint
    conn.close()
