"""Playlist ownership upgrades preserve existing mirrors and validate their guards."""
import pytest

from yt_playlist.core.migrations import MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def test_v46_upgrade_preserves_the_mirror_and_observes_older_connection_writes(tmp_path):
    store = Store(tmp_path / 'installed.db')
    try:
        migrate(store.conn, registry=MIGRATIONS[:46])
        with store.conn:
            store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'Main','bridge',1)")
            store.conn.execute("INSERT INTO playlist_scopes VALUES ('PL','PL',NULL,'shared')")
            store.conn.execute("INSERT INTO playlist_membership_refs(id,scope_ref,identity_id) VALUES (7,'PL',1)")
            store.conn.execute("INSERT INTO playlists(id,identity_id,ytm_playlist_id,title,track_count,"
                               "content_hash,first_seen,last_seen,last_changed,scope_ref) "
                               "VALUES (7,1,'PL','Keep me',0,'hash',10,20,10,'PL')")
            store.conn.execute("INSERT INTO rec_vectors VALUES ('song|artist',x'01020304')")
            store.conn.execute("INSERT INTO settings VALUES ('personal-setting','keep')")
        before = tuple(store.conn.execute('SELECT * FROM playlists').fetchone())
        store.init_schema()
        assert tuple(store.conn.execute('SELECT * FROM playlists').fetchone()) == before
        assert store.rec_model.get_rec_vectors() == [('song|artist', b'\x01\x02\x03\x04')]
        assert store.settings.get_setting('personal-setting') == 'keep'
        initial = store.playlists.playlist_revision(1, 'PL')
        # Even SQL issued by an older process now advances the persisted owner.
        with store.conn:
            store.conn.execute("UPDATE playlists SET title='Newer' WHERE id=7")
        assert store.playlists.publish_playlist_snapshot(1, 'PL', 'Old response', [], 100,
                                                        expected_revision=initial) is None
        assert store.playlists.get_playlist(7).title == 'Newer'
        revision = store.playlists.playlist_revision(1, 'PL')
        store.init_schema()
        assert store.playlists.playlist_revision(1, 'PL') == revision
        assert not (tmp_path / 'backups').exists()
    finally:
        store.conn.close()


def test_missing_publication_guard_is_rejected_by_the_current_schema(store):
    with store.conn:
        store.conn.execute('DROP TRIGGER playlist_revision_playlist_items_insert')
    with pytest.raises(MigrationError, match='playlist_revision_playlist_items_insert'):
        store.init_schema()
