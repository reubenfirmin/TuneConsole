"""Retire replacement-model state without losing sources or last-good results."""
import sqlite3

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, current, migrate, registry
from yt_playlist.core.migrations import v039_background_refresh as refresh_migration


def predecessor(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn, registry=registry.MIGRATIONS[:38])
    conn.execute("INSERT INTO rec_vectors VALUES ('saved', X'0000803F')")
    conn.execute("INSERT INTO settings VALUES ('rec_content_model','saved model')")
    conn.execute("INSERT INTO settings VALUES ('rec_selection_active_run','42')")
    conn.execute("INSERT INTO rec_proposals(surface,payload,built_at) VALUES ('mode_bundles','{}',1)")
    conn.execute("INSERT INTO recordings(identity_key,title,artist,genre) VALUES ('song','Song','Artist','Techno')")
    conn.execute("INSERT INTO rec_learning_state VALUES ('enabled','1')")
    conn.commit()
    return conn


def test_upgrade_preserves_original_models_sources_and_caches_and_coalesces_changes(tmp_path):
    with predecessor(tmp_path / 'state.db') as conn:
        migrate(conn)
        current.validate(conn)
        assert tuple(conn.execute('SELECT * FROM rec_vectors').fetchone()) == ('saved', bytes.fromhex('0000803F'))
        assert conn.execute("SELECT value FROM settings WHERE key='rec_content_model'").fetchone()[0] == 'saved model'
        assert conn.execute("SELECT value FROM settings WHERE key='rec_selection_active_run'").fetchone()[0] == '42'
        assert conn.execute("SELECT payload FROM rec_proposals WHERE surface='mode_bundles'").fetchone()[0] == '{}'
        assert conn.execute("SELECT genre FROM recordings WHERE identity_key='song'").fetchone()[0] == 'Techno'
        for table in refresh_migration.RETIRED_TABLES:
            assert conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone() is None
        revision = conn.execute('SELECT revision FROM rec_change_queue').fetchone()[0]
        conn.execute("UPDATE recordings SET genre='Techno' WHERE identity_key='song'")
        assert conn.execute('SELECT revision FROM rec_change_queue').fetchone()[0] == revision
        conn.execute("UPDATE recordings SET genre='Jazz' WHERE identity_key='song'")
        assert list(map(tuple, conn.execute('SELECT * FROM rec_change_queue'))) == [('refresh', '', revision + 1)]
        conn.rollback()
        assert conn.execute('SELECT revision FROM rec_change_queue').fetchone()[0] == revision
        before = list(conn.iterdump())
        migrate(conn)
        assert list(conn.iterdump()) == before


def test_interrupted_retirement_rolls_back_schema_and_models(tmp_path, monkeypatch):
    with predecessor(tmp_path / 'state.db') as conn:
        before = list(conn.iterdump())
        original = registry.MIGRATIONS
        def interrupted(connection, context):
            refresh_migration.apply(connection, context)
            raise RuntimeError('interrupted refresh upgrade')
        monkeypatch.setattr(registry, 'MIGRATIONS', (*original[:38], Migration(
            39, original[38].name, interrupted)))
        with pytest.raises(MigrationError, match='interrupted refresh upgrade'):
            migrate(conn)
        assert list(conn.iterdump()) == before
        monkeypatch.setattr(registry, 'MIGRATIONS', original)
        migrate(conn)
        current.validate(conn)


def test_upgrade_retires_hashed_encoder_and_surrogate_selection_without_dropping_saved_results(tmp_path):
    with predecessor(tmp_path / 'hashed.db') as conn:
        conn.execute("UPDATE settings SET value=? WHERE key='rec_content_model'",
                     ('{"cat":{},"ncat":128,"cont":[],"hash_bins":128}',))
        conn.execute("UPDATE settings SET value='progressive' WHERE key='rec_selection_active_run'")
        conn.commit()
        migrate(conn)
        current.validate(conn)
        assert not conn.execute("SELECT 1 FROM settings WHERE key IN "
                                "('rec_content_model','rec_selection_active_run')").fetchone()
        assert tuple(conn.execute('SELECT * FROM rec_vectors').fetchone()) == ('saved', bytes.fromhex('0000803F'))
        assert conn.execute("SELECT payload FROM rec_proposals WHERE surface='mode_bundles'").fetchone()[0] == '{}'
        assert conn.execute("SELECT kind FROM rec_change_queue").fetchone()[0] == 'refresh'
