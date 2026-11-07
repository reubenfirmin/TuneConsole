"""Adopt checkpoint guards without rewriting installed conversion history."""
import sqlite3

import pytest

from tests.core.test_independent_schema_changes import installed, legacy_history
from yt_playlist.core import group_checkpoint_retention
from yt_playlist.core.migrations import migrate, registry, regular


@pytest.mark.parametrize('checkpoint_owner_exists', [False, True])
def test_checkpoint_guard_adoption_and_later_owner_upgrade(installed, checkpoint_owner_exists):
    with sqlite3.connect(installed) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if checkpoint_owner_exists:
            migrate(conn, registry=registry.MIGRATIONS[:94])
        regular.migrate(conn, changes=regular.CHANGES[:25])
        before = legacy_history(conn)
        applied = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        statements = []
        conn.set_trace_callback(statements.append)
        regular.migrate(conn)
        conn.set_trace_callback(None)
        assert legacy_history(conn) == before
        assert list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))[:25] == applied
        assert not any(sql.lstrip().upper().startswith(('SELECT * FROM GROUP_', 'DELETE FROM GROUP_'))
                       for sql in statements)
        if not checkpoint_owner_exists:
            migrate(conn, registry=registry.MIGRATIONS[:94])
        group_checkpoint_retention.validate(conn)
        guards = list(group_checkpoint_retention.definitions(conn))
        assert len(guards) == 4
        epoch = conn.execute('SELECT revision FROM group_retention_epoch').fetchone()[0]
        regular.migrate(conn)
        assert conn.execute('SELECT revision FROM group_retention_epoch').fetchone()[0] == epoch
        regular.validate(conn)
