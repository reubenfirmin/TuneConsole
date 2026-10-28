"""Real SQLite compaction with controlled disk limits and handoff failures."""
from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_space, temporary_compaction


@pytest.fixture
def database(tmp_path, monkeypatch):
    directory = tmp_path / 'library with spaces'
    directory.mkdir()
    scratch = tmp_path / 'OS temporary files'
    scratch.mkdir()
    monkeypatch.setattr(temporary_compaction.tempfile, 'tempdir', str(scratch))
    path = directory / 'state.db'
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT);'
        'CREATE TABLE activity(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id), payload BLOB);'
        'CREATE TABLE discarded(payload BLOB);')
    with conn:
        conn.execute("INSERT INTO parent VALUES (1,'preserved')")
        conn.executemany('INSERT INTO activity VALUES (?,1,?)', [(i, bytes([i % 256]) * 4000) for i in range(96)])
        conn.executemany('INSERT INTO discarded VALUES (?)', [(b'x' * 32768,)] * 128)
    with conn:
        conn.execute('DELETE FROM discarded')
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    try:
        yield conn, path, scratch
    finally:
        conn.close()


def _personal(conn):
    return (conn.execute('SELECT * FROM parent ORDER BY id').fetchall(),
            conn.execute('SELECT * FROM activity ORDER BY id').fetchall())


def test_os_temporary_output_fits_when_two_compact_images_would_not(database, monkeypatch):
    conn, path, scratch = database
    expected, original_bytes = _personal(conn), path.stat().st_size
    page_size = conn.execute('PRAGMA page_size').fetchone()[0]
    used = (conn.execute('PRAGMA page_count').fetchone()[0]
            - conn.execute('PRAGMA freelist_count').fetchone()[0]) * page_size
    available = db_space.MIN_FREE_BYTES + temporary_compaction.COPY_ALLOWANCE + used + used // 100 + 32
    assert available < db_space.MIN_FREE_BYTES + 2 * used + temporary_compaction.COPY_ALLOWANCE
    monkeypatch.setattr(db_space.shutil, 'disk_usage', lambda directory: SimpleNamespace(
        free=available if Path(directory) == path.parent else 2 * 1024**3))
    statements = []
    conn.set_trace_callback(statements.append)
    with closing(sqlite3.connect(path)) as existing:
        assert _personal(existing) == expected
        temporary_compaction.compact(conn, path.parent)
        assert _personal(existing) == expected
        assert existing.execute('PRAGMA foreign_key_check').fetchall() == []
        with existing:
            existing.execute("INSERT INTO parent VALUES (2,'new activity')")
        assert conn.execute('SELECT name FROM parent WHERE id=2').fetchone()[0] == 'new activity'
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    assert path.stat().st_size < original_bytes / 2
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    vacuum = next(statement for statement in statements if statement.startswith('VACUUM INTO'))
    assert str(scratch) in vacuum
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize('location', ['temporary', 'database'])
def test_insufficient_space_preserves_database_and_cleans_scratch(database, monkeypatch, location):
    conn, path, scratch = database
    expected, original_bytes = _personal(conn), path.stat().st_size
    def space(directory):
        is_database = Path(directory) == path.parent
        insufficient = is_database if location == 'database' else not is_database
        return SimpleNamespace(free=1024 if insufficient else 2 * 1024**3)
    monkeypatch.setattr(db_space.shutil, 'disk_usage', space)
    with pytest.raises(db_space.LowDiskSpace):
        temporary_compaction.compact(conn, path.parent)
    assert _personal(conn) == expected and path.stat().st_size == original_bytes
    assert list(scratch.iterdir()) == []
    with conn:
        conn.execute("INSERT INTO parent VALUES (2,'recording resumes')")


def test_verification_failure_never_installs_candidate(database, monkeypatch):
    conn, path, scratch = database
    expected, original_bytes = _personal(conn), path.stat().st_size
    def invalid(candidate):
        raise ValueError('injected invalid candidate')
    monkeypatch.setattr(temporary_compaction, '_verify', invalid)
    with pytest.raises(ValueError, match='invalid candidate'):
        temporary_compaction.compact(conn, path.parent)
    assert _personal(conn) == expected and path.stat().st_size == original_bytes
    assert list(scratch.iterdir()) == []


def test_external_commit_in_handoff_is_preserved_and_retry_uses_fresh_data(database, monkeypatch):
    conn, path, scratch = database
    original = temporary_compaction._restore
    def intervening_writer(candidate, path, observed, version, cancelled):
        with closing(sqlite3.connect(path)) as other, other:
            other.execute("INSERT INTO parent VALUES (2,'committed during handoff')")
        return original(candidate, path, observed, version, cancelled)
    monkeypatch.setattr(temporary_compaction, '_restore', intervening_writer)
    with pytest.raises(sqlite3.OperationalError, match='changed during compaction handoff') as failure:
        temporary_compaction.compact(conn, path.parent)
    assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert conn.execute('SELECT name FROM parent WHERE id=2').fetchone()[0] == 'committed during handoff'
    assert list(scratch.iterdir()) == []
    monkeypatch.setattr(temporary_compaction, '_restore', original)
    temporary_compaction.compact(conn, path.parent)
    assert conn.execute('SELECT name FROM parent WHERE id=2').fetchone()[0] == 'committed during handoff'
    assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize('failure', ['cancelled', 'full', 'io_error'])
def test_failure_after_partial_backup_rolls_back_destination(database, monkeypatch, failure):
    conn, path, scratch = database
    expected = _personal(conn)
    original_open = temporary_compaction._open
    cancelled, partial = False, False

    class FailingCandidate(sqlite3.Connection):
        def backup(self, target, *, pages, progress, sleep):
            def injected(status, remaining, total):
                nonlocal cancelled, partial
                if status == sqlite3.SQLITE_OK:
                    assert 0 < remaining < total
                    partial = True
                    if failure == 'cancelled':
                        cancelled = True
                    else:
                        code = sqlite3.SQLITE_FULL if failure == 'full' else sqlite3.SQLITE_IOERR_WRITE
                        raise temporary_compaction._error('injected partial backup failure', code)
                progress(status, remaining, total)
            return super().backup(target, pages=pages, progress=injected, sleep=sleep)

    def opened(candidate_path, *, readonly=False):
        if candidate_path.name == 'compact.db' and readonly:
            return sqlite3.connect(candidate_path.as_uri() + '?mode=ro&cache=private', uri=True,
                                   factory=FailingCandidate)
        return original_open(candidate_path, readonly=readonly)
    monkeypatch.setattr(temporary_compaction, '_open', opened)
    with pytest.raises(sqlite3.OperationalError):
        temporary_compaction.compact(conn, path.parent, cancelled=lambda: cancelled)
    assert partial and _personal(conn) == expected
    assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert list(scratch.iterdir()) == []
    with conn:
        conn.execute("INSERT INTO parent VALUES (2,'recording resumes')")


def test_busy_writer_wait_is_bounded_and_does_not_install(database, monkeypatch):
    conn, path, scratch = database
    expected = _personal(conn)
    original = temporary_compaction._restore
    monkeypatch.setattr(temporary_compaction, 'WRITER_WAIT_SECONDS', 0)
    def busy(candidate, path, observed, version, cancelled):
        with closing(sqlite3.connect(path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            return original(candidate, path, observed, version, cancelled)
    monkeypatch.setattr(temporary_compaction, '_restore', busy)
    with pytest.raises(sqlite3.OperationalError, match='writer is busy'):
        temporary_compaction.compact(conn, path.parent)
    assert _personal(conn) == expected and list(scratch.iterdir()) == []


def test_readers_remain_consistent_until_checkpoint_can_reclaim_space(database, monkeypatch):
    conn, path, scratch = database
    expected, original_bytes = _personal(conn), path.stat().st_size
    original = temporary_compaction._restore
    with closing(sqlite3.connect(path)) as reader:
        def pinned(candidate, path, observed, version, cancelled):
            reader.execute('BEGIN')
            assert _personal(reader) == expected
            return original(candidate, path, observed, version, cancelled)
        monkeypatch.setattr(temporary_compaction, '_restore', pinned)
        temporary_compaction.compact(conn, path.parent)
        assert _personal(reader) == expected and _personal(conn) == expected
        conn.execute('PRAGMA busy_timeout=1')
        assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 1
        reader.rollback()
    assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    assert path.stat().st_size < original_bytes / 2 and list(scratch.iterdir()) == []
