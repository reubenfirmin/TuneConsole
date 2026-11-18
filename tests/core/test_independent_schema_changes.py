"""Post-release DDL keeps each bounded change and the frozen history atomic."""
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from tests.release_schema_support import release_schema
from yt_playlist.core import schema_inspection
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.migrations.__main__ import main


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL and CLI checks do not construct an HTTP application."""


@pytest.fixture
def installed(tmp_path):
    path = tmp_path / 'released.db'
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        release_schema(conn)
        conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
    return path


def legacy_history(conn):
    return list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))


def test_sample_status_adoption_preserves_rows_and_files_without_conversion(installed, monkeypatch):
    from yt_playlist.core import fao, sample_files
    from yt_playlist.repos.audio_vibes import validate_sample_files
    value = dict(id='installed-sample', run_id='installed-run', point='thirty', video_id='video')
    filename = sample_files.publish(installed.parent / 'sample-attributes', value)
    original_file = Path(filename).read_bytes()
    with closing(sqlite3.connect(installed)) as conn:
        version = next(c.version for c in regular.CHANGES if c.name == 'record_owned_audio_sample_files')
        regular.migrate(conn, changes=regular.CHANGES[:version - 1])
        with conn:
            rid = conn.execute("INSERT INTO recordings(identity_key,title,created_at) VALUES ('sample','Song',1)").lastrowid
            conn.execute('INSERT INTO youtube_items(video_id,recording_id) VALUES (?,?)', ('video', rid))
            conn.execute('INSERT INTO sample_attributes VALUES (?,?,?,?,?,?,?)',
                         (value['id'], rid, 'video', value['run_id'], 'thirty', filename, 1))
        original_row = conn.execute('SELECT * FROM sample_attributes').fetchone()
        numbered = legacy_history(conn)
        read, write = Mock(side_effect=AssertionError('DDL read a managed file')), Mock(
            side_effect=AssertionError('DDL wrote a managed file'))
        with monkeypatch.context() as patch:
            patch.setattr(fao, 'read', read)
            patch.setattr(fao, 'write', write)
            regular.migrate(conn)
            validate_sample_files(conn)
        assert conn.execute('SELECT * FROM sample_attributes').fetchone() == (*original_row, None, 'ready')
        assert legacy_history(conn) == numbered
        assert Path(filename).read_bytes() == original_file
        read.assert_not_called()
        write.assert_not_called()
        validate_version(conn, 107)


def test_interrupted_sample_status_adoption_rolls_back_and_retries(installed, monkeypatch):
    from yt_playlist.repos import audio_vibes
    with closing(sqlite3.connect(installed)) as conn:
        version = next(c.version for c in regular.CHANGES if c.name == 'record_owned_audio_sample_files')
        regular.migrate(conn, changes=regular.CHANGES[:version - 1])
        before = list(conn.iterdump())
        install = audio_vibes.install_sample_files
        def interrupted(*args):
            install(*args)
            raise OSError('interrupted sample status installation')
        with monkeypatch.context() as patch:
            patch.setattr(audio_vibes, 'install_sample_files', interrupted)
            with pytest.raises(OSError, match='interrupted sample status'):
                regular.migrate(conn)
        assert list(conn.iterdump()) == before
        regular.migrate(conn)
        audio_vibes.validate_sample_files(conn)


def test_regular_failure_keeps_prior_commits_and_applied_history(installed, monkeypatch):
    def interrupted(conn, context):
        conn.execute('CREATE TABLE interrupted_owner(id INTEGER PRIMARY KEY)')
        raise RuntimeError('Interrupted regular DDL')
    with closing(sqlite3.connect(installed)) as conn, conn:
        before = legacy_history(conn)
        next_version = len(regular.CHANGES) + 1
        changes = (*regular.CHANGES, regular.Change(next_version, 'later_addition', interrupted, lambda conn: None))
        with pytest.raises(RuntimeError, match='Interrupted regular DDL'):
            regular.migrate(conn, changes=changes)
        assert legacy_history(conn) == before
        assert conn.execute('SELECT count(*) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='interrupted_owner'").fetchone()
        successor = regular.Change(next_version, 'later_addition',
            lambda conn, context: conn.execute('CREATE TABLE later_owner(id INTEGER PRIMARY KEY)'),
            lambda conn: conn.execute('SELECT id FROM later_owner LIMIT 0'))
        from tests.release_schema_support import future_schema
        future_schema(monkeypatch, 'later_addition', 'CREATE TABLE later_owner(id INTEGER PRIMARY KEY)')
        regular.migrate(conn, changes=(*regular.CHANGES, successor))
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == next_version
        assert legacy_history(conn) == before


def test_regular_runner_preserves_caller_transaction(installed):
    with closing(sqlite3.connect(installed)) as conn, conn:
        conn.execute("UPDATE settings SET value='uncommitted' WHERE key='retained-setting'")
        with pytest.raises(MigrationError, match='outstanding caller transaction'):
            regular.migrate(conn)
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT value FROM settings WHERE key='retained-setting'").fetchone() == ('original',)


def test_regular_cli_preserves_numbered_history(installed):
    with closing(sqlite3.connect(installed)) as conn:
        before = legacy_history(conn)
    assert main(['upgrade-schema', str(installed)]) == 0
    with closing(sqlite3.connect(installed)) as conn:
        assert legacy_history(conn) == before
        assert conn.execute('SELECT count(*) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)


def test_competing_regular_startups_install_once(installed):
    barrier, errors = threading.Barrier(2), []
    def start():
        try:
            with closing(sqlite3.connect(installed, timeout=10)) as conn:
                barrier.wait()
                regular.migrate(conn)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors


def test_completed_validation_leaves_writer_available(installed, monkeypatch):
    with closing(sqlite3.connect(installed)) as conn:
        regular.migrate(conn)
    entered, release = threading.Event(), threading.Event()
    validate, errors = regular.validate_version, []
    def slow(*args, **kwargs):
        result = validate(*args, **kwargs)
        entered.set()
        assert release.wait(15)
        return result
    monkeypatch.setattr(regular, 'validate_version', slow)
    def inspect():
        try:
            with closing(sqlite3.connect(installed, timeout=.1)) as conn:
                regular.migrate(conn)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=inspect)
    worker.start()
    try:
        assert entered.wait(15)
        with closing(sqlite3.connect(installed, timeout=.1)) as conn, conn:
            conn.execute("UPDATE settings SET value='continuing writer' WHERE key='retained-setting'")
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive() and not errors
