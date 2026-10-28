"""Forward recovery from the actual development version 34 schema."""
import sqlite3

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, current, migrate, registry
from yt_playlist.core.migrations import v035_progressive_source_members as source_migration


def predecessor(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn, registry=registry.MIGRATIONS[:34])
    conn.execute("INSERT INTO settings VALUES ('rec_content_model','saved-model')")
    conn.execute("INSERT INTO rec_vectors VALUES ('saved-key',X'0000803F')")
    conn.execute("INSERT INTO rec_learning_state VALUES ('checkpoint','12')")
    conn.execute("INSERT INTO rec_local_nodes VALUES ('saved-key',X'0000803F',X'00000000',4,'saved-digest',100)")
    conn.executemany('INSERT INTO rec_evidence VALUES (?,?,?,?,?)', [
        ('playlist', 'one', 'playlist:1', 'saved-key', 1),
        ('playlist', 'two', 'playlist:1', 'saved-key', 0.5),
        ('track', 'three', 'metadata', 'saved-key', 0),
    ])
    conn.execute("INSERT INTO rec_change_queue VALUES ('node','saved-key',3)")
    conn.commit()
    return conn


def test_applied_34_upgrades_without_losing_model_ledger_or_checkpoint(tmp_path):
    with predecessor(tmp_path / 'applied.db') as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='ix_rec_playlist_scope_cursor'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='rec_source_members'").fetchone() is None
        migrate(conn, registry=registry.MIGRATIONS[:35])
        current.validate(conn, version=35)
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 35
        assert conn.execute("SELECT value FROM settings WHERE key='rec_content_model'").fetchone()[0] == 'saved-model'
        assert tuple(conn.execute('SELECT * FROM rec_vectors').fetchone()) == ('saved-key', bytes.fromhex('0000803F'))
        assert conn.execute("SELECT value FROM rec_learning_state WHERE key='checkpoint'").fetchone()[0] == '12'
        assert tuple(conn.execute('SELECT steps,graph_digest FROM rec_local_nodes').fetchone()) == (4, 'saved-digest')
        assert conn.execute('SELECT COUNT(*) FROM rec_evidence').fetchone()[0] == 3
        assert list(map(tuple, conn.execute('SELECT * FROM rec_source_members'))) == [('playlist:1', 'saved-key', 1.5, 0)]
        assert conn.execute("SELECT revision FROM rec_change_queue WHERE kind='node' AND item='saved-key'").fetchone()[0] == 4
        before = list(conn.iterdump())
        migrate(conn, registry=registry.MIGRATIONS[:35])
        assert list(conn.iterdump()) == before


def test_34_with_additions_already_present_keeps_projection_and_queue(tmp_path):
    with predecessor(tmp_path / 'expanded.db') as conn:
        for sql in source_migration.statements():
            conn.execute(sql)
        conn.execute("INSERT INTO rec_source_members VALUES ('playlist:1','saved-key',1.5,123)")
        conn.commit()
        migrate(conn, registry=registry.MIGRATIONS[:35])
        current.validate(conn, version=35)
        assert tuple(conn.execute('SELECT * FROM rec_source_members').fetchone()) == ('playlist:1', 'saved-key', 1.5, 123)
        assert conn.execute("SELECT revision FROM rec_change_queue WHERE kind='node' AND item='saved-key'").fetchone()[0] == 3


def test_interrupted_forward_repair_rolls_back_schema_and_data(tmp_path, monkeypatch):
    with predecessor(tmp_path / 'interrupted.db') as conn:
        before = list(conn.iterdump())
        original = registry.MIGRATIONS

        def interrupted(connection, context):
            source_migration.apply(connection, context)
            raise RuntimeError('interrupted source projection')

        monkeypatch.setattr(registry, 'MIGRATIONS', (*original[:34], Migration(35, original[34].name, interrupted)))
        with pytest.raises(MigrationError, match='interrupted source projection'):
            migrate(conn)
        assert list(conn.iterdump()) == before
        monkeypatch.setattr(registry, 'MIGRATIONS', original)
        migrate(conn)
        current.validate(conn)


def test_forward_repair_rejects_an_incompatible_existing_index(tmp_path):
    with predecessor(tmp_path / 'incompatible.db') as conn:
        conn.execute('CREATE INDEX ix_rec_playlist_scope_cursor ON playlists(id)')
        conn.commit()
        before = list(conn.iterdump())
        with pytest.raises(MigrationError, match='Incompatible current index: ix_rec_playlist_scope_cursor'):
            migrate(conn)
        assert list(conn.iterdump()) == before
