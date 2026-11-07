"""Frozen SQL-policy predecessor for supported adapter contract checks."""
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.store import Store


def open_legacy(path):
    store = Store(path)
    # This is an explicit migration fixture, outside ordinary admission.
    migrate(store.conn, registry=registry.MIGRATIONS[:69])
    store._bind_repositories()
    store._schema_compatibility = True
    store.conn.execute('PRAGMA journal_mode=WAL')
    store._snapshot_revisions = Revisions(store.conn)
    return store
