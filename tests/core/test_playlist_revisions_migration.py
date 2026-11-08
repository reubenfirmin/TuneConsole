"""Playlist ownership upgrades preserve existing mirrors and validate their guards."""
import pytest

from yt_playlist.core.migrations import MigrationError




def test_missing_publication_guard_is_rejected_by_the_current_schema(store):
    with store.conn:
        store.conn.execute('DROP TRIGGER playlist_revision_playlist_items_insert')
    with pytest.raises(MigrationError, match='playlist_revision_playlist_items_insert'):
        store.init_schema()
