"""Independent retirement DDL preserves the installed conversion history."""
from contextlib import closing
import sqlite3

import pytest

from tests.core.test_independent_schema_changes import legacy_history
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import group_retention_progress as progress
from yt_playlist.core.migrations import regular, v072_compact_groups
from yt_playlist.core.migrations.current import _tokens
from yt_playlist.core.migrations.runner import validate_version


@pytest.fixture(scope='module')
def retirement_predecessor():
    with closing(sqlite3.connect(':memory:')) as conn, pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        conn.row_factory = sqlite3.Row
        install_numbered_input(conn, 79)
        with conn:
            conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Listener','credential',1)")
            conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
        regular.migrate(conn, changes=regular.CHANGES[:5])
        conn.execute('PRAGMA query_only=ON')
        yield conn


@pytest.fixture
def installed(retirement_predecessor, tmp_path, monkeypatch):
    # Change six owns the retirement DDL. Other changes have their own tests.
    monkeypatch.setattr(regular, 'CHANGES', regular.CHANGES[:6])
    path = tmp_path / 'retirement.db'
    with closing(sqlite3.connect(path)) as conn:
        retirement_predecessor.backup(conn)
    return path


def test_retirement_input_keeps_installed_history_and_completed_lower_changes(retirement_predecessor):
    conn = retirement_predecessor
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
    assert [tuple(row) for row in conn.execute('SELECT version,name FROM schema_changes ORDER BY version')] == [
        (change.version, change.name) for change in regular.CHANGES[:5]]
    assert not progress.available(conn)
    assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='ix_group_evidence_version'").fetchone()
    validate_version(conn, 79)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_install_only_adds_empty_work_owners_and_reuses_existing_evidence_index(installed):
    with closing(sqlite3.connect(installed)) as conn:
        before = legacy_history(conn)
        evidence_index = conn.execute("SELECT sql FROM sqlite_schema WHERE name='ix_group_evidence_version'").fetchone()[0]
        statements = []
        conn.set_trace_callback(statements.append)
        regular.migrate(conn)
        conn.set_trace_callback(None)
        assert legacy_history(conn) == before
        assert conn.execute('SELECT * FROM group_retention_epoch').fetchall() == [(1, 0)]
        for table in ('group_retention_walk', 'group_retention_generations',
                      'group_retention_aliases', 'group_retention_edges'):
            assert not conn.execute(f'SELECT 1 FROM {table}').fetchone()
        created = [sql for sql in statements if sql.startswith('CREATE INDEX ix_group_retention_')]
        assert len(created) == 2
        assert all(' ON group_retention_' in sql for sql in created)
        assert conn.execute("SELECT sql FROM sqlite_schema WHERE name='ix_group_evidence_version'").fetchone()[0] == evidence_index
        validate_version(conn, 79)
        progress.validate(conn)
        regular.migrate(conn)
        assert legacy_history(conn) == before


def test_interrupted_install_restores_old_guard_and_resumes_without_stamping(installed, monkeypatch):
    with closing(sqlite3.connect(installed)) as conn:
        before = legacy_history(conn)
        guard = conn.execute("SELECT sql FROM sqlite_schema WHERE name='retained_group_publication_delete'").fetchone()[0]
        original = progress.install

        def interrupt(conn):
            original(conn)
            raise RuntimeError('interrupted retirement DDL')

        with monkeypatch.context() as patch:
            patch.setattr(progress, 'install', interrupt)
            with pytest.raises(RuntimeError, match='interrupted retirement DDL'):
                regular.migrate(conn)
        assert legacy_history(conn) == before
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 5
        assert not progress.available(conn)
        assert conn.execute("SELECT sql FROM sqlite_schema WHERE name='retained_group_publication_delete'").fetchone()[0] == guard
        v072_compact_groups.validate(conn)
        regular.migrate(conn)
        progress.validate(conn)
        assert legacy_history(conn) == before
        assert _tokens(conn.execute("SELECT sql FROM sqlite_schema WHERE name='retained_group_publication_delete'").fetchone()[0]) == _tokens(progress.publication_guard())
