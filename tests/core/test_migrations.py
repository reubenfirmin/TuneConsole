import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager

import pytest

from tests.release_schema_support import future_schema
from yt_playlist.core.migrations import MigrationError, migrate, regular
from yt_playlist.core.store import Store


@contextmanager
def connect(path):
    with closing(sqlite3.connect(path, timeout=2)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            yield conn


def installed(conn):
    migrate(conn)
    with conn:
        conn.execute("INSERT INTO settings VALUES ('evidence','original')")


def successor(apply):
    return (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1, 'next_owner', apply,
        lambda conn: conn.execute('SELECT id FROM next_owner LIMIT 0')))


def test_failure_rolls_back_ddl_data_and_version_then_resumes(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    def broken(conn, ctx):
        conn.execute('CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
        conn.execute("UPDATE settings SET value='lost' WHERE key='evidence'")
        raise ValueError('injected interruption')
    with connect(path) as conn:
        installed(conn)
        before = list(conn.iterdump())
        with pytest.raises(ValueError, match='injected interruption'):
            regular.migrate(conn, changes=successor(broken))
    with connect(path) as reopened:
        assert list(reopened.iterdump()) == before
        future_schema(monkeypatch, 'next_owner', 'CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
        def completed(conn, ctx):
            conn.execute('CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
            conn.execute("UPDATE settings SET value='done' WHERE key='evidence'")
        regular.migrate(reopened, changes=successor(completed))
        assert reopened.execute("SELECT value FROM settings WHERE key='evidence'").fetchone() == ('done',)
        assert reopened.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES) + 1


def test_baseline_failure_removes_history_and_all_changes(tmp_path, monkeypatch):
    from yt_playlist.core import schema_bootstrap
    bootstrap = schema_bootstrap.bootstrap
    def broken(*args, **kwargs):
        bootstrap(*args, **kwargs)
        raise KeyboardInterrupt()
    monkeypatch.setattr(schema_bootstrap, 'bootstrap', broken)
    with connect(tmp_path / 'state.db') as conn:
        with pytest.raises(KeyboardInterrupt):
            migrate(conn)
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == []


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_upgrade_rolls_back_and_commits_without_a_backup_destination(tmp_path, journal, monkeypatch):
    path = tmp_path / 'state.db'
    # A file blocks creation of a backup directory. Migration safety must come
    # from the transaction even when there is nowhere to store a snapshot.
    blocked = tmp_path / 'backups'
    blocked.write_text('existing file; leave untouched')
    with connect(path) as conn:
        conn.execute(f'PRAGMA journal_mode={journal}')
        installed(conn)
        before = list(conn.iterdump())
        def interrupted(connection, context):
            connection.execute('CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
            connection.execute("DELETE FROM settings WHERE key='evidence'")
            raise RuntimeError('interrupted replacement')
        with pytest.raises(RuntimeError, match='interrupted replacement'):
            regular.migrate(conn, changes=successor(interrupted))
        assert list(conn.iterdump()) == before
        future_schema(monkeypatch, 'next_owner', 'CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
        def completed(connection, context):
            connection.execute('CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
            connection.execute("DELETE FROM settings WHERE key='evidence'")
        regular.migrate(conn, changes=successor(completed))
    with connect(path) as reopened:
        assert reopened.execute("SELECT value FROM settings WHERE key='evidence'").fetchone() is None
        assert reopened.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES) + 1
        assert reopened.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert blocked.read_text() == 'existing file; leave untouched'
    assert list(tmp_path.glob('*.db')) == [path]


def test_rejects_caller_transaction_without_committing_or_rolling_it_back():
    with connect(':memory:') as conn:
        conn.execute('CREATE TABLE data (v INTEGER)')
        conn.execute('INSERT INTO data VALUES (1)')
        with pytest.raises(MigrationError, match='outstanding'):
            migrate(conn)
        assert conn.in_transaction
        conn.rollback()
        assert not conn.execute('SELECT * FROM data').fetchall()


@pytest.mark.parametrize('sql', [
    'CREATE TABLE schema_migrations (version TEXT)',
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL); INSERT INTO schema_migrations VALUES (99,'future',1)",
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL); INSERT INTO schema_migrations VALUES (2,'gap',1)",
])
def test_invalid_database_unchanged(sql):
    with connect(':memory:') as conn:
        conn.executescript(sql)
        before = list(conn.iterdump())
        with pytest.raises(MigrationError):
            migrate(conn)
        assert list(conn.iterdump()) == before


def test_competing_startups_apply_once(tmp_path):
    path = tmp_path / 'state.db'
    barrier = threading.Barrier(2)
    errors = []
    def run():
        try:
            with connect(path) as conn:
                barrier.wait()
                migrate(conn)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert all(not t.is_alive() for t in threads)
    assert not errors
    with connect(path) as conn:
        assert conn.execute('SELECT max(version),count(*) FROM schema_migrations').fetchone() == (107, 107)
        assert conn.execute('SELECT count(*) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_process_death_rolls_back(tmp_path, journal, monkeypatch):
    path = tmp_path / 'state.db'
    with connect(path) as conn:
        conn.execute(f'PRAGMA journal_mode={journal}')
        installed(conn)
        before = list(conn.iterdump())
    source = '''import os,sqlite3,sys
from yt_playlist.core.migrations import regular
def interrupted(conn, context):
    conn.execute('CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
    conn.execute("UPDATE settings SET value='lost' WHERE key='evidence'")
    os._exit(7)
c=sqlite3.connect(sys.argv[1])
regular.migrate(c,changes=(*regular.CHANGES,regular.Change(len(regular.CHANGES)+1,'next_owner',interrupted,lambda c: None)))
'''
    assert subprocess.run([sys.executable,'-c',source,str(path)], timeout=20).returncode == 7
    with connect(path) as conn:
        assert list(conn.iterdump()) == before
        future_schema(monkeypatch, 'next_owner', 'CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
        def completed(connection, context):
            connection.execute('CREATE TABLE next_owner(id INTEGER PRIMARY KEY)')
            connection.execute("UPDATE settings SET value='done' WHERE key='evidence'")
        regular.migrate(conn, changes=successor(completed))
        assert conn.execute("SELECT value FROM settings WHERE key='evidence'").fetchone() == ('done',)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not (tmp_path / 'backups').exists()


def test_fresh_and_current_create_no_backup(tmp_path):
    store = Store(tmp_path / 'fresh.db')
    with closing(store.conn):
        store.init_schema()
        store.init_schema()
        assert not (tmp_path / 'backups').exists()


def test_corrupted_versioned_schema_is_not_silently_repaired(tmp_path):
    store = Store(tmp_path / 'state.db')
    with closing(store.conn):
        store.init_schema()
        store.conn.execute('DROP TABLE rec_mode_picks')
        with pytest.raises(MigrationError, match='rec_mode_picks'):
            store.init_schema()
