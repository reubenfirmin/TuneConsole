from yt_playlist.library.album_catalog import remember_album


def album():
    return {"title": "Release", "artists": [{"name": "Artist"}], "trackCount": 4,
            "tracks": [{"title": f"Track {i}", "artists": [{"name": "Artist"}]} for i in range(4)]}


def test_complete_tracklist_is_separate_from_incidental_tracks(store):
    store.upsert_track("v", "Track 0", "Artist", "Release", 100, album_browse_id="MPRE")
    assert store.collection.cached_album_tracklist_ids() == set()
    remember_album(store, "MPRE", album())
    assert store.collection.cached_album_tracklist_ids() == {"MPRE"}


def test_partial_album_responses_cannot_set_a_denominator(store):
    value = album()
    value["tracks"].pop()
    remember_album(store, "partial", value)
    value = album()
    value["continuations"] = ["next page"]
    remember_album(store, "continuation", value)
    remember_album(store, "empty", {})
    assert store.collection.cached_album_tracklist_ids() == set()


def test_metadata_only_album_entries_keep_source_links_without_ownership(store):
    remember_album(store,'release-a',album())
    remember_album(store,'release-a',album())
    remember_album(store,'release-b',album())
    assert store.conn.execute('SELECT COUNT(*) FROM unresolved_items').fetchone()[0] == 8
    assert store.conn.execute('SELECT COUNT(*) FROM album_track_links').fetchone()[0] == 8
    assert not store.library_keys()
    assert not store.conn.execute('SELECT 1 FROM youtube_items').fetchone()
