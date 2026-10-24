"""Existing action journals survive the deletion-admission index upgrade."""
import sqlite3

from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS


def test_upgrade_keeps_action_journals_and_indexes_only_pending_deletions(tmp_path):
    with sqlite3.connect(tmp_path / "actions.db") as conn:
        migrate(conn, registry=MIGRATIONS[:44])
        conn.executemany(
            "INSERT INTO actions(kind,status,undo_json) VALUES ('delete_playlist',?,?)",
            [("delete_pending", '{"deletions":[{"identity":7,"ytm":"SOURCE"}]}'),
             ("executed", '{"backup":"saved.json"}')])
        conn.commit()
        before = conn.execute("SELECT * FROM actions ORDER BY id").fetchall()

        migrate(conn, registry=MIGRATIONS[:45])

        assert conn.execute("SELECT * FROM actions ORDER BY id").fetchall() == before
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 45
        assert conn.execute("SELECT partial FROM pragma_index_list('actions') "
                            "WHERE name='ix_actions_delete_pending'").fetchone() == (1,)
        conn.execute("UPDATE actions SET status='executed' WHERE id=1")
        conn.commit()
        assert conn.execute("SELECT id FROM actions INDEXED BY ix_actions_delete_pending "
                            "WHERE status='delete_pending'").fetchall() == []
        migrate(conn, registry=MIGRATIONS[:45])
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 2
