import pytest

from yt_playlist.core.album_queries import TRACK_LINKS
from yt_playlist.library.album_catalog import remember_album


def album():
    return {"title": "Release", "artists": [{"name": "Artist"}], "trackCount": 4,
            "tracks": [{"title": f"Track {i}", "artists": [{"name": "Artist"}]} for i in range(4)]}


def test_complete_tracklist_is_separate_from_incidental_tracks(store):
    store.tracks.upsert_track("v", "Track 0", "Artist", "Release", 100, album_browse_id="MPRE")
    assert not store.collection.has_published_album_tracklist("MPRE")
    remember_album(store, "MPRE", album())
    assert store.collection.has_published_album_tracklist("MPRE")


def test_partial_album_responses_cannot_set_a_denominator(store):
    value = album()
    value["tracks"].pop()
    remember_album(store, "partial", value)
    value = album()
    value["continuations"] = ["next page"]
    remember_album(store, "continuation", value)
    remember_album(store, "empty", {})
    assert not store.conn.execute('SELECT 1 FROM albums WHERE current_tracklist_id IS NOT NULL').fetchone()


def test_metadata_only_album_entries_keep_source_links_without_ownership(store):
    remember_album(store,'release-a',album())
    remember_album(store,'release-a',album())
    remember_album(store,'release-b',album())
    assert store.conn.execute('SELECT COUNT(*) FROM unresolved_items').fetchone()[0] == 8
    assert store.conn.execute(f'SELECT COUNT(*) FROM {TRACK_LINKS}').fetchone()[0] == 8
    assert not store.rec_query.library_keys()
    assert not store.conn.execute('SELECT 1 FROM track_sources WHERE video_id IS NOT NULL').fetchone()


@pytest.mark.parametrize('order', [(None, 'release'), ('release', None)])
def test_missing_album_id_cannot_create_an_unlinked_duplicate(store, order):
    for browse in order:
        entry = store.tracks.upsert_track('v', 'Song', 'Artist', 'Release', 100,
                                          album_browse_id=browse)
        if browse is None and order[0] is None:
            assert not store.conn.execute('SELECT 1 FROM albums').fetchone()
        assert store.conn.execute('SELECT album FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 'Release'
    assert store.conn.execute('SELECT COUNT(*) FROM albums').fetchone()[0] == 1
    assert list(map(tuple, store.conn.execute(
        'SELECT p.provider_id,t.entry_id FROM album_tracks t JOIN album_provider_ids p ON p.album_id=t.album_id'
    ))) == [('release', entry)]
    assert not store.collection.saved_album_ids()
