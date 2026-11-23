"""Schema and disk failure diagnostics for shared policies."""
import sqlite3
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_space
from yt_playlist.core.migrations.runner import MigrationError


def test_current_schema_rejects_missing_shared_policy_structures(tmp_path, consumer_store):
    store = consumer_store(tmp_path / 'state.db')
    store.conn.execute('DROP INDEX ix_recommendation_policy_base')
    with pytest.raises(MigrationError, match='ix_recommendation_policy_base'):
        store.init_schema()


def test_io_failure_diagnostic_names_database_and_available_space(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / 'state.db')
    error = sqlite3.OperationalError('disk I/O error')
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR
    monkeypatch.setattr(db_space.shutil, 'disk_usage', lambda path: SimpleNamespace(free=4096))
    hint = db_space.failure_hint(conn, error)
    assert str(tmp_path / 'state.db') in hint
    assert '0.0 MiB' in hint and 'preserve' in hint
    conn.close()
