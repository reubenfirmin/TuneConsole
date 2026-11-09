"""Private numbered SQL below publication, with populated-format validation."""
from contextlib import closing, contextmanager
import sqlite3

import pytest

from tests.store_fixture_support import _numbered_input, reuse_reporting_reader
from yt_playlist.core import schema_inspection
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.store import Store


@contextmanager
def installed_reader(path, monkeypatch):
    """Real startup and a real reporting connection for publication readers."""
    database = Store(path)
    with closing(database.conn), schema_inspection.reuse_tables(database.conn):
        # Disposable inputs keep real commits and rollback without power-loss sync.
        database.conn.execute('PRAGMA synchronous=OFF')
        database.init_schema(defer_metadata=True)
        with reuse_reporting_reader(database, monkeypatch) as reader:
            with schema_inspection.reuse_tables(reader.conn):
                yield database


@pytest.fixture
def installed(request, tmp_path, monkeypatch):
    """Build one private guarded input rather than an intermediate Store copy."""
    version = request.param
    with _numbered_input(version, tmp_path, monkeypatch, snapshot_guards=True) as value:
        store, _ = value
        history = list(map(tuple, store.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version')))
        assert [(row[0], row[1]) for row in history] == [
            (entry.version, entry.name) for entry in registry.MIGRATIONS[:version]]
        assert store._snapshot_revisions.conn is store.conn
        assert store.conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
        yield value
        assert list(map(tuple, store.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version'))) == history
        # Serving TEMP views intentionally shadow historical physical layouts.
        # Validate committed populated SQL through a separate physical reader.
        with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            with schema_inspection.reuse_tables(reader):
                runner.validate_version(reader, version)
                assert not reader.execute('PRAGMA foreign_key_check').fetchone()
