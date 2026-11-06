"""A retained Home read cannot hide newer listening or release membership."""
import pytest
from yt_playlist.core.albums import observe


@pytest.mark.parametrize('include_generated', [False, True])
def test_recency_reopens_after_history_and_album_membership_updates(file_backed_store, include_generated):
    store = file_backed_store
    account = store.accounts.upsert_account('main', 'cred', None, True)
    entry = store.tracks.upsert_track('song', 'Song', 'Artist', '', 180)
    key = store.tracks.identity_key_for_video('song')
    store.collection.add_saved_album({'browse': 'album', 'title': 'Release', 'artist': 'Artist'})
    with store.conn:
        album = observe(store.conn, 'album', title='Release', artist='Artist', rank=1)
        store.conn.execute("INSERT INTO album_item_observations(album_id,entry_id,provider) VALUES (?,?,'youtube')",
                           (album, entry))
    store.history.record_history_plays(account, 86400, [key], source_timezone='UTC')
    expected = 86400 + 43200 + (86400 if include_generated else 0)

    def read(reader):
        return reader.collection.saved_albums_recency(
            browse_ids=['album'], include_generated=include_generated)

    with store.read_view(tracked=True) as older:
        assert read(older) == {'album': expected}
        store.history.record_history_plays(account, 172800, [key], source_timezone='UTC')
        with store.read_view(tracked=True) as newer:
            assert read(newer) == {'album': expected + 86400}
        assert read(older) == {'album': expected}
    store.collection.remove_saved_album('album')
    with store.read_view(tracked=True) as reader:
        assert read(reader) == {}
