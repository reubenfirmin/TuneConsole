"""Verify one populated released database can upgrade through normal startup."""
from contextlib import closing

from tests.genre_catalog_support import bound_catalog
from tests.release_schema_support import release_schema
from yt_playlist.core.migrations import regular
from yt_playlist.core.store import Store


def test_release_upgrade_retains_rows_and_applied_history(tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    store = Store(tmp_path / 'released.db')
    with closing(store.conn) as conn:
        with conn:
            release_schema(conn)
            conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
            conn.execute("INSERT INTO recordings(identity_key,title,created_at) VALUES ('retained','Song',1)")
        numbered_history = [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        regular_history = [tuple(row) for row in conn.execute('SELECT * FROM schema_changes ORDER BY version')]
        assert len(regular_history) < len(regular.CHANGES)

        store.init_schema()

        assert [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == numbered_history
        applied = [tuple(row) for row in conn.execute('SELECT * FROM schema_changes ORDER BY version')]
        assert applied[:len(regular_history)] == regular_history
        assert len(applied) == len(regular.CHANGES)
        assert conn.execute("SELECT title FROM recordings WHERE identity_key='retained'").fetchone()[0] == 'Song'
        assert store.settings.get_setting('retained-setting') == 'original'
        assert not store.activity_paused
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        store.settings.set_setting('continuing-write', 'accepted')
        assert store.settings.get_setting('continuing-write') == 'accepted'
