"""Independent retirement DDL preserves the installed conversion history."""
import sqlite3

import pytest

from tests.core.test_independent_schema_changes import installed, legacy_history
from yt_playlist.core import group_retention_progress as progress
from yt_playlist.core.migrations import regular, v072_compact_groups
from yt_playlist.core.migrations.current import _tokens
from yt_playlist.core.migrations.runner import validate_version


def test_install_only_adds_empty_work_owners_and_reuses_existing_evidence_index(installed):
    with sqlite3.connect(installed) as conn:
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
    with sqlite3.connect(installed) as conn:
        regular.migrate(conn, changes=tuple(change for change in regular.CHANGES if change.version < 6))
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
