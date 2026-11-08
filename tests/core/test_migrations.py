import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.store import Store


@contextmanager
def connect(path):
    with closing(sqlite3.connect(path, timeout=2)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            yield conn


def test_failure_rolls_back_ddl_data_and_version_then_resumes(tmp_path):
    path = tmp_path / 'state.db'
    def first(conn, ctx):
        conn.execute('CREATE TABLE evidence (value TEXT)')
        conn.execute("INSERT INTO evidence VALUES ('original')")
    def broken(conn, ctx):
        conn.execute('ALTER TABLE evidence ADD COLUMN changed INTEGER')
        conn.execute("UPDATE evidence SET value='lost'")
        raise ValueError('injected interruption')
    registry = (Migration(1,'first',first), Migration(2,'second',broken))
    with connect(path) as conn:
        with pytest.raises(MigrationError, match='2 .*second.*injected'):
            migrate(conn, registry=registry)
    with connect(path) as reopened:
        assert reopened.execute('SELECT * FROM evidence').fetchall() == [('original',)]
        assert reopened.execute('SELECT version FROM schema_migrations').fetchall() == [(1,)]
        migrate(reopened, registry=(registry[0],Migration(2,'second',lambda c,x: c.execute("UPDATE evidence SET value='done'"))))
        assert reopened.execute('SELECT * FROM evidence').fetchall() == [('done',)]


def test_baseline_failure_removes_history_and_all_changes(tmp_path):
    def broken(conn, ctx):
        conn.execute('CREATE TABLE half_ready (id INTEGER)')
        raise KeyboardInterrupt()
    with connect(tmp_path / 'state.db') as conn:
        with pytest.raises(KeyboardInterrupt):
            migrate(conn, registry=[Migration(1,'broken',broken)])
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == []


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_upgrade_rolls_back_and_commits_without_a_backup_destination(tmp_path, journal):
    path = tmp_path / 'state.db'
    # A file blocks creation of a backup directory. Migration safety must come
    # from the transaction even when there is nowhere to store a snapshot.
    blocked = tmp_path / 'backups'
    blocked.write_text('existing file; leave untouched')
    with connect(path) as conn:
        conn.execute(f'PRAGMA journal_mode={journal}')
        conn.execute('CREATE TABLE data (v INTEGER)')
        conn.execute('INSERT INTO data VALUES (42)')
        conn.commit()
        def interrupted(connection, context):
            connection.execute('ALTER TABLE data ADD COLUMN changed INTEGER')
            connection.execute('DELETE FROM data')
            raise RuntimeError('interrupted replacement')
        with pytest.raises(MigrationError, match='interrupted replacement'):
            migrate(conn, registry=[Migration(1, 'replace', interrupted)])
        assert conn.execute('SELECT * FROM data').fetchall() == [(42,)]
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        migrate(conn, registry=[Migration(1, 'replace', lambda c,x: c.execute('DELETE FROM data'))])
    with connect(path) as reopened:
        assert reopened.execute('SELECT * FROM data').fetchall() == []
        assert reopened.execute('SELECT version FROM schema_migrations').fetchall() == [(1,)]
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
    'CREATE TABLE unrelated (id INTEGER)',
    'CREATE TABLE tracks (id TEXT PRIMARY KEY)',
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
                migrate(conn, registry=[Migration(1,'once',lambda c,x: c.execute('CREATE TABLE once_only (id INTEGER)'))])
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with connect(path) as conn:
        assert conn.execute('SELECT version FROM schema_migrations').fetchall() == [(1,)]


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_process_death_rolls_back(tmp_path, journal):
    path = tmp_path / 'state.db'
    def first(conn, context):
        conn.execute('CREATE TABLE evidence (value TEXT)')
        conn.execute("INSERT INTO evidence VALUES ('original')")
    with connect(path) as conn:
        conn.execute(f'PRAGMA journal_mode={journal}')
        migrate(conn, registry=[Migration(1, 'first', first)])
    source = '''import os,sqlite3,sys
from yt_playlist.core.migrations import Migration,migrate
def interrupted(conn, context):
    conn.execute('ALTER TABLE evidence ADD COLUMN changed INTEGER')
    conn.execute("UPDATE evidence SET value='lost'")
    os._exit(7)
c=sqlite3.connect(sys.argv[1])
migrate(c,registry=[Migration(1,'first',lambda c,x: None),Migration(2,'second',interrupted)])
'''
    assert subprocess.run([sys.executable,'-c',source,str(path)]).returncode == 7
    with connect(path) as conn:
        assert conn.execute('SELECT * FROM evidence').fetchall() == [('original',)]
        assert conn.execute('SELECT version FROM schema_migrations').fetchall() == [(1,)]
        migrate(conn, registry=[Migration(1, 'first', first), Migration(
            2, 'second', lambda c,x: c.execute("UPDATE evidence SET value='done'"))])
        assert conn.execute('SELECT * FROM evidence').fetchall() == [('done',)]
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
