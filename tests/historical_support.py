"""Explicit replay for genuine predecessor fixtures, outside ordinary startup."""
from yt_playlist.core.migrations import migrate, registry


def upgrade_fixture(store):
    migrate(store.conn, registry=registry.MIGRATIONS)
    store.init_schema()
