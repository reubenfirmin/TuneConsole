"""DAO suite for CollectionRepo (saved-album CRUD + aggregate library views)."""


def _album(browse, **kw):
    return {"browse": browse, "title": kw.get("title", "T"), "artist": kw.get("artist", "A"),
            "year": kw.get("year"), "type": kw.get("type", "Album"), "thumbnail": kw.get("thumbnail")}


def test_playback_source_uses_exact_playlist_and_prefers_master_copy(store):
    other = store.identities.upsert_identity("other", "c", None, False)
    master = store.identities.upsert_identity("main", "c", None, True)
    store.playlists.upsert_playlist(other, "PL1", "Other copy", 1, "h", 0)
    pid = store.playlists.upsert_playlist(master, "PL1", "Playing mix", 1, "h", 0)
    unrelated = store.playlists.upsert_playlist(master, "PL2", "Also contains song", 1, "h", 0)
    tid = store.tracks.upsert_track("v1", "Song", "Artist", "Release", 200, album_browse_id="MPRE_album")
    store.playlists.set_playlist_tracks(unrelated, [tid])
    assert store.collection.playback_source("PL1", "v1", "MPRE_album") == {
        "kind": "playlist", "id": pid, "title": "Playing mix"}
    assert store.collection.playback_source("PL_unknown", "v1", "MPRE_album") is None
    assert store.collection.playback_source(None, "unknown") is None


def test_saved_album_crud(store):
    store.collection.add_saved_album(_album("B1", title="One", artist="Alpha"))
    store.collection.add_saved_album(_album("B2", title="Two", artist="Beta"))
    assert store.collection.saved_album_ids() == {"B1", "B2"}
    # sorted by artist then title
    assert [a["browse"] for a in store.collection.get_saved_albums()] == ["B1", "B2"]
    store.collection.remove_saved_album("B1")
    assert store.collection.saved_album_ids() == {"B2"}


def test_saved_albums_recency_newest_play_per_album(store):
    from yt_playlist.util.matching import identity_key
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.collection.add_saved_album(_album("HOT", title="Hot"))
    store.collection.add_saved_album(_album("COLD", title="Cold"))
    store.collection.add_saved_album(_album("NEVER", title="Never"))
    # HOT: two tracks, played at 100 and 300 -> album's newest play is 300
    store.tracks.upsert_track("vh1", "H1", "AH", None, None, album_browse_id="HOT")
    store.tracks.upsert_track("vh2", "H2", "AH", None, None, album_browse_id="HOT")
    store.history.add_history_snapshot(iid, 100.0, [identity_key("H1", "AH")])
    store.history.add_history_snapshot(iid, 300.0, [identity_key("H2", "AH")])
    # COLD: one track, played once long ago
    store.tracks.upsert_track("vc1", "C1", "AC", None, None, album_browse_id="COLD")
    store.history.add_history_snapshot(iid, 50.0, [identity_key("C1", "AC")])
    # NEVER: a track exists in the library but was never played
    store.tracks.upsert_track("vn1", "N1", "AN", None, None, album_browse_id="NEVER")

    rec = store.collection.saved_albums_recency()
    assert rec["HOT"] == 300.0       # newest play across the album's tracks
    assert rec["COLD"] == 50.0
    assert rec["NEVER"] is None      # never played -> coldest


def test_replace_saved_albums_is_wholesale(store):
    store.collection.add_saved_album(_album("OLD"))
    store.collection.replace_saved_albums([_album("N1"), _album("N2"), {"title": "no-browse"}])
    assert store.collection.saved_album_ids() == {"N1", "N2"}   # OLD gone, browse-less skipped


def test_collection_albums_aggregates_plays_and_playlists(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    p1 = store.playlists.upsert_playlist(iid, "PL1", "List 1", 0, "h", 0.0)
    p2 = store.playlists.upsert_playlist(iid, "PL2", "List 2", 0, "h", 0.0)
    t = store.tracks.upsert_track("v1", "Song", "Artist", "The Album", 200)
    store.playlists.set_playlist_tracks(p1, [t])
    store.playlists.set_playlist_tracks(p2, [t])                          # same track in two playlists
    store.history.add_history_snapshot(iid, 1.0, ["v1" and store.conn.execute(
        "SELECT identity_key k FROM tracks WHERE id=?", (t,)).fetchone()["k"]])
    albums = store.collection.collection_albums()
    alb = next(a for a in albums if a["album"] == "The Album")
    assert alb["artist"] == "Artist" and alb["songs"] == 1 and alb["n_pls"] == 2 and alb["plays"] == 1


def test_collection_artists_includes_saved_album_tracks_without_playlists(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.collection.add_saved_album(_album("B1", title="The Album", artist="Artist"))
    store.tracks.upsert_track("v1", "Song", "Artist", "The Album", 200,
                       album_browse_id="B1", thumbnail="/cover.png")
    store.history.add_history_snapshot(iid, 1.0, ["song|artist", "song|artist"])

    assert store.collection.collection_artists() == [{
        "artist": "Artist", "browse": None, "songs": 1, "plays": 2,
        "n_albums": 1, "n_pls": 0, "thumbnail": "/cover.png"}]


def test_collection_artists_merges_saved_albums_without_double_counting_songs(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    p1 = store.playlists.upsert_playlist(iid, "PL1", "List 1", 0, "h", 0.0)
    p2 = store.playlists.upsert_playlist(iid, "PL2", "List 2", 0, "h", 0.0)
    track = store.tracks.upsert_track("v1", "Song", "Artist", "Original", 200,
                               artist_browse_id="UC_artist", album_browse_id="B1")
    store.playlists.set_playlist_tracks(p1, [track])
    store.playlists.set_playlist_tracks(p2, [track])
    # Album sync doesn't always have the artist ID; a second release can contain the same song.
    other = store.tracks.upsert_track("v2", "Song", "Artist", "Compilation", 200, album_browse_id="B2")
    store.tracks.associate_youtube_item('v2',store.tracks.catalog_subject(track)['recording_id'],
                                source='fixture_verified',evidence={'recording':'same performance'},now=1)
    store.tracks.upsert_track("v3", "Album only", "Artist", "Compilation", 200, album_browse_id="B2")
    store.collection.add_saved_album(_album("B1", title="Original", artist="Artist"))
    store.collection.add_saved_album(_album("B2", title="Compilation", artist="Artist"))
    store.history.add_history_snapshot(iid, 1.0, [store.tracks.catalog_subject(track)['identity_key'], "album only|artist"])

    artist, = store.collection.collection_artists()
    assert artist["browse"] == "UC_artist"
    assert (artist["songs"], artist["n_albums"], artist["n_pls"], artist["plays"]) == (2, 2, 2, 2)


def test_collection_artists_includes_saved_album_metadata_before_tracks_arrive(store):
    store.collection.add_saved_album(_album("B1", title="One", artist="Artist", thumbnail="/cover.png"))
    store.collection.add_saved_album(_album("B2", title="Two", artist="Artist"))
    store.collection.add_saved_album(_album("B3", artist=""))
    store.collection.add_saved_album(_album("B4", artist=None))

    assert store.collection.collection_artists() == [{
        "artist": "Artist", "browse": None, "songs": 0, "plays": 0,
        "n_albums": 2, "n_pls": 0, "thumbnail": "/cover.png"}]


def test_collection_artists_excludes_unsaved_album_tracks_and_keeps_playlist_artists(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    playlist = store.playlists.upsert_playlist(iid, "PL1", "Mix", 0, "h", 0.0)
    track = store.tracks.upsert_track("v1", "Song", "Playlist artist", "One", 200, album_browse_id="B1")
    store.playlists.set_playlist_tracks(playlist, [track])
    store.tracks.upsert_track("v2", "Other", "Album artist", "Two", 200, album_browse_id="B2")
    store.tracks.upsert_track("v3", "Cached", "Unowned artist", "Three", 200, album_browse_id="B3")
    store.collection.add_saved_album(_album("B1", title="One", artist="Playlist artist"))
    store.collection.add_saved_album(_album("B2", title="Two", artist="Album artist"))
    assert {a["artist"] for a in store.collection.collection_artists()} == {"Playlist artist", "Album artist"}

    store.collection.remove_saved_album("B1")
    store.collection.remove_saved_album("B2")
    assert [a["artist"] for a in store.collection.collection_artists()] == ["Playlist artist"]


def test_collection_artists_keeps_same_named_artists_and_releases_distinct(store):
    store.collection.add_saved_album(_album("B1", title="Self-titled", artist="Shared Name"))
    store.collection.add_saved_album(_album("B2", title="Self-titled", artist="Shared Name"))
    store.collection.add_saved_album(_album("B3", title="Self-titled", artist="Shared Name"))
    store.tracks.upsert_track("v1", "One", "Shared Name", "Self-titled", 200,
                       artist_browse_id="UC_first", album_browse_id="B1")
    store.tracks.upsert_track("v2", "Two", "Shared Name", "Self-titled", 200,
                       artist_browse_id="UC_second", album_browse_id="B2")
    store.tracks.upsert_track("v3", "Three", "Shared Name", "Self-titled", 200,
                       artist_browse_id="UC_first", album_browse_id="B3")

    artists = {a["browse"]: a for a in store.collection.collection_artists()}
    assert set(artists) == {"UC_first", "UC_second"}
    assert (artists["UC_first"]["songs"], artists["UC_first"]["n_albums"]) == (2, 2)
    assert (artists["UC_second"]["songs"], artists["UC_second"]["n_albums"]) == (1, 1)


def test_artist_browse_id_picks_most_common(store):
    store.identities.upsert_identity("me", "c", None, True)
    store.tracks.upsert_track("v1", "S1", "Artist", "Al", 100, artist_browse_id="CH_A")
    store.tracks.upsert_track("v2", "S2", "Artist", "Al", 100, artist_browse_id="CH_A")
    store.tracks.upsert_track("v3", "S3", "Artist", "Al", 100, artist_browse_id="CH_B")
    assert store.collection.artist_browse_id("Artist") == "CH_A"
    assert store.collection.artist_browse_id("Nobody") is None


def test_store_binding(store):
    store.collection.add_saved_album(_album("B1"))
    assert store.collection.saved_album_ids() == {"B1"}
    assert store.collection.collection_albums() == []                     # no playlists → no albums
