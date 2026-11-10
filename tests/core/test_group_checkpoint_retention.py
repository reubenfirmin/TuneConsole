"""Adopt checkpoint guards without rewriting installed conversion history."""
import sqlite3
from contextlib import closing

import pytest

from tests.core.test_independent_schema_changes import legacy_history
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, install_regular_input, mock_regular_numbered_input
from yt_playlist.core import group_checkpoint_retention, schema_inspection
from yt_playlist.core.migrations import migrate, registry, regular


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Checkpoint SQL never constructs the HTTP app or its background workers."""


@pytest.fixture
def installed(tmp_path, monkeypatch):
    # This owner covers checkpoint v94 before/after regular guard adoption.
    # Earlier numbered owners are completed lower input in both orderings.
    bound_catalog(monkeypatch)
    path = tmp_path / 'checkpoint-predecessor.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        install_numbered_input(conn, 93)
        with conn:
            conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Listener','credential',1)")
            conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
    return path


@pytest.mark.parametrize('checkpoint_owner_exists', [False, True])
def test_checkpoint_guard_adoption_and_later_owner_upgrade(installed, monkeypatch, checkpoint_owner_exists):
    with closing(sqlite3.connect(installed)) as conn, schema_inspection.reuse_tables(conn):
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA foreign_keys=ON')
        if checkpoint_owner_exists:
            migrate(conn, registry=registry.MIGRATIONS[:94])
        # The first 25 regular owners precede checkpoint-guard adoption. Supply
        # their completed input; the real runner owns every later change below.
        with conn:
            conn.execute('BEGIN')
            install_regular_input(conn, regular.CHANGES[:25])
        assert regular.validate(conn) == 25
        before = legacy_history(conn)
        applied = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        statements = []
        conn.set_trace_callback(statements.append)
        with monkeypatch.context() as fixed_input:
            mock_regular_numbered_input(fixed_input, conn)
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
        with monkeypatch.context() as fixed_input:
            # The real v94 conversion above changes the numbered contract.
            # Admit that new input afresh before checking guard idempotence.
            mock_regular_numbered_input(fixed_input, conn)
            regular.migrate(conn)
        assert conn.execute('SELECT revision FROM group_retention_epoch').fetchone()[0] == epoch
        assert regular.validate(conn) == len(regular.CHANGES)
