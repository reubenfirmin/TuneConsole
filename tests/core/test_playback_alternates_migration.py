"""Installed catalogs retain their exact upload facts when playback aliases are added."""
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def test_upgrade_retains_catalog_and_accepts_account_scoped_playback_aliases(tmp_path):
    store = Store(tmp_path / 'installed.db')
    try:
        migrate(store.conn, registry=MIGRATIONS[:47])
        store._bind_repositories()
        identity = store.identities.upsert_identity('Main', 'ref', None, True)
        store.tracks.upsert_track('original', 'Song', 'Artist', '', 100)
        before = tuple(store.conn.execute('SELECT * FROM youtube_items').fetchone())
        store.init_schema()
        assert tuple(store.conn.execute('SELECT * FROM youtube_items').fetchone()) == before
        assert store.tracks.playback_alternate(identity, 'original') is None
        store.tracks.upsert_track('replacement', 'Song', 'Artist', '', 100)
        original = {'video_id': 'original', 'title': 'Song', 'artist': 'Artist', 'duration': 100}
        store.tracks.remember_playback_alternate(identity, original,
            {**original, 'video_id': 'replacement'}, evidence={'set_video_id': 'owned'}, now=1)
        store.init_schema()
        assert store.tracks.playback_alternate(identity, 'original')['video_id'] == 'replacement'
    finally:
        store.conn.close()
