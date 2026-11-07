"""Explicit replay for genuine predecessor fixtures, outside ordinary startup."""
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.db_revisions import Revisions


def upgrade_fixture(store):
    # Seeding may use snapshot readers on the predecessor. Their TEMP triggers
    # reference its columns and must be removed before replaying schema changes.
    Revisions.unbind(store.conn)
    migrate(store.conn, registry=registry.MIGRATIONS)
    store.init_schema()
