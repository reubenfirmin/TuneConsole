"""The emergency upgrade is additive and never copies the bloated policy table."""
import sqlite3
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_space
from yt_playlist.core.migrations.runner import MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def test_shared_policy_upgrade_preserves_legacy_payload_without_backup_or_rewrite(tmp_path):
    path = tmp_path / 'state.db'
    conn = sqlite3.connect(path)
    migrate(conn, registry=MIGRATIONS[:52])
    raw = '{"ranker":"legacy","evidence":"' + 'x' * 1000000 + '"}'
    conn.execute('INSERT INTO rec_serving_policies VALUES (?, ?, 1)', ('legacy-policy', raw))
    conn.execute('INSERT INTO settings VALUES (?,?)', ('active-session', '{"pool_policy":"legacy-policy"}'))
    conn.commit()
    pages = conn.execute('PRAGMA page_count').fetchone()[0]
    migrate(conn, registry=MIGRATIONS[:53])
    assert conn.execute('SELECT configuration FROM rec_serving_policies').fetchone()[0] == raw
    assert conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0] == 0
    assert conn.execute('PRAGMA page_count').fetchone()[0] - pages < 32
    assert not (tmp_path / 'backups').exists()
    conn.close()
    store = Store(path)
    store.init_schema()
    try:
        assert store.rec_ledger.policy_configuration('legacy-policy')['ranker'] == 'legacy'
    finally:
        store.conn.close()


def test_current_schema_rejects_missing_shared_policy_structures(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    store.conn.execute('DROP INDEX ix_rec_policy_base')
    with pytest.raises(MigrationError, match='ix_rec_policy_base'):
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
