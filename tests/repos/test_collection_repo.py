"""DAO suite for CollectionRepo (saved-album CRUD + aggregate library views)."""


def _album(browse, **kw):
    return {"browse": browse, "title": kw.get("title", "T"), "artist": kw.get("artist", "A"),
            "year": kw.get("year"), "type": kw.get("type", "Album"), "thumbnail": kw.get("thumbnail")}


def test_playback_source_uses_exact_playlist_and_prefers_master_copy(store):
    other = store.upsert_identity("other", "c", None, False)
    master = store.upsert_identity("main", "c", None, True)
    store.upsert_playlist(other, "PL1", "Other copy", 1, "h", 0)
    pid = store.upsert_playlist(master, "PL1", "Playing mix", 1, "h", 0)
    unrelated = store.upsert_playlist(master, "PL2", "Also contains song", 1, "h", 0)
    tid = store.upsert_track("v1", "Song", "Artist", "Release", 200, album_browse_id="MPRE_album")
    store.set_playlist_tracks(unrelated, [tid])
    assert store.playback_source("PL1", "v1", "MPRE_album") == {
        "kind": "playlist", "id": pid, "title": "Playing mix"}
    assert store.playback_source("PL_unknown", "v1", "MPRE_album") is None
    assert store.playback_source(None, "unknown") is None


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
    iid = store.upsert_identity("me", "c", None, True)
    store.collection.add_saved_album(_album("HOT", title="Hot"))
    store.collection.add_saved_album(_album("COLD", title="Cold"))
    store.collection.add_saved_album(_album("NEVER", title="Never"))
    # HOT: two tracks, played at 100 and 300 -> album's newest play is 300
    store.upsert_track("vh1", "H1", "AH", None, None, album_browse_id="HOT")
    store.upsert_track("vh2", "H2", "AH", None, None, album_browse_id="HOT")
    store.add_history_snapshot(iid, 100.0, [identity_key("H1", "AH")])
    store.add_history_snapshot(iid, 300.0, [identity_key("H2", "AH")])
    # COLD: one track, played once long ago
    store.upsert_track("vc1", "C1", "AC", None, None, album_browse_id="COLD")
    store.add_history_snapshot(iid, 50.0, [identity_key("C1", "AC")])
    # NEVER: a track exists in the library but was never played
    store.upsert_track("vn1", "N1", "AN", None, None, album_browse_id="NEVER")

    rec = store.saved_albums_recency()
    assert rec["HOT"] == 300.0       # newest play across the album's tracks
    assert rec["COLD"] == 50.0
    assert rec["NEVER"] is None      # never played -> coldest


def test_replace_saved_albums_is_wholesale(store):
    store.collection.add_saved_album(_album("OLD"))
    store.collection.replace_saved_albums([_album("N1"), _album("N2"), {"title": "no-browse"}])
    assert store.collection.saved_album_ids() == {"N1", "N2"}   # OLD gone, browse-less skipped


def test_collection_albums_aggregates_plays_and_playlists(store):
    iid = store.upsert_identity("me", "c", None, True)
    p1 = store.upsert_playlist(iid, "PL1", "List 1", 0, "h", 0.0)
    p2 = store.upsert_playlist(iid, "PL2", "List 2", 0, "h", 0.0)
    t = store.upsert_track("v1", "Song", "Artist", "The Album", 200)
    store.set_playlist_tracks(p1, [t])
    store.set_playlist_tracks(p2, [t])                          # same track in two playlists
    store.add_history_snapshot(iid, 1.0, ["v1" and store.conn.execute(
        "SELECT identity_key k FROM tracks WHERE id=?", (t,)).fetchone()["k"]])
    albums = store.collection.collection_albums()
    alb = next(a for a in albums if a["album"] == "The Album")
    assert alb["artist"] == "Artist" and alb["songs"] == 1 and alb["n_pls"] == 2 and alb["plays"] == 1


def test_collection_artists_includes_saved_album_tracks_without_playlists(store):
    iid = store.upsert_identity("me", "c", None, True)
    store.add_saved_album(_album("B1", title="The Album", artist="Artist"))
    store.upsert_track("v1", "Song", "Artist", "The Album", 200,
                       album_browse_id="B1", thumbnail="/cover.png")
    store.add_history_snapshot(iid, 1.0, ["song|artist", "song|artist"])

    assert store.collection_artists() == [{
        "artist": "Artist", "browse": None, "songs": 1, "plays": 2,
        "n_albums": 1, "n_pls": 0, "thumbnail": "/cover.png"}]


def test_collection_artists_merges_saved_albums_without_double_counting_songs(store):
    iid = store.upsert_identity("me", "c", None, True)
    p1 = store.upsert_playlist(iid, "PL1", "List 1", 0, "h", 0.0)
    p2 = store.upsert_playlist(iid, "PL2", "List 2", 0, "h", 0.0)
    track = store.upsert_track("v1", "Song", "Artist", "Original", 200,
                               artist_browse_id="UC_artist", album_browse_id="B1")
    store.set_playlist_tracks(p1, [track])
    store.set_playlist_tracks(p2, [track])
    # Album sync doesn't always have the artist ID; a second release can contain the same song.
    store.upsert_track("v2", "Song", "Artist", "Compilation", 200, album_browse_id="B2")
    store.upsert_track("v3", "Album only", "Artist", "Compilation", 200, album_browse_id="B2")
    store.add_saved_album(_album("B1", title="Original", artist="Artist"))
    store.add_saved_album(_album("B2", title="Compilation", artist="Artist"))
    store.add_history_snapshot(iid, 1.0, ["song|artist", "album only|artist"])

    artist, = store.collection_artists()
    assert artist["browse"] == "UC_artist"
    assert (artist["songs"], artist["n_albums"], artist["n_pls"], artist["plays"]) == (2, 2, 2, 2)


def test_collection_artists_includes_saved_album_metadata_before_tracks_arrive(store):
    store.add_saved_album(_album("B1", title="One", artist="Artist", thumbnail="/cover.png"))
    store.add_saved_album(_album("B2", title="Two", artist="Artist"))
    store.add_saved_album(_album("B3", artist=""))
    store.add_saved_album(_album("B4", artist=None))

    assert store.collection_artists() == [{
        "artist": "Artist", "browse": None, "songs": 0, "plays": 0,
        "n_albums": 2, "n_pls": 0, "thumbnail": "/cover.png"}]


def test_collection_artists_excludes_unsaved_album_tracks_and_keeps_playlist_artists(store):
    iid = store.upsert_identity("me", "c", None, True)
    playlist = store.upsert_playlist(iid, "PL1", "Mix", 0, "h", 0.0)
    track = store.upsert_track("v1", "Song", "Playlist artist", "One", 200, album_browse_id="B1")
    store.set_playlist_tracks(playlist, [track])
    store.upsert_track("v2", "Other", "Album artist", "Two", 200, album_browse_id="B2")
    store.upsert_track("v3", "Cached", "Unowned artist", "Three", 200, album_browse_id="B3")
    store.add_saved_album(_album("B1", title="One", artist="Playlist artist"))
    store.add_saved_album(_album("B2", title="Two", artist="Album artist"))
    assert {a["artist"] for a in store.collection_artists()} == {"Playlist artist", "Album artist"}

    store.remove_saved_album("B1")
    store.remove_saved_album("B2")
    assert [a["artist"] for a in store.collection_artists()] == ["Playlist artist"]


def test_collection_artists_keeps_same_named_artists_and_releases_distinct(store):
    store.add_saved_album(_album("B1", title="Self-titled", artist="Shared Name"))
    store.add_saved_album(_album("B2", title="Self-titled", artist="Shared Name"))
    store.add_saved_album(_album("B3", title="Self-titled", artist="Shared Name"))
    store.upsert_track("v1", "One", "Shared Name", "Self-titled", 200,
                       artist_browse_id="UC_first", album_browse_id="B1")
    store.upsert_track("v2", "Two", "Shared Name", "Self-titled", 200,
                       artist_browse_id="UC_second", album_browse_id="B2")
    store.upsert_track("v3", "Three", "Shared Name", "Self-titled", 200,
                       artist_browse_id="UC_first", album_browse_id="B3")

    artists = {a["browse"]: a for a in store.collection_artists()}
    assert set(artists) == {"UC_first", "UC_second"}
    assert (artists["UC_first"]["songs"], artists["UC_first"]["n_albums"]) == (2, 2)
    assert (artists["UC_second"]["songs"], artists["UC_second"]["n_albums"]) == (1, 1)


def test_artist_browse_id_picks_most_common(store):
    store.upsert_identity("me", "c", None, True)
    store.upsert_track("v1", "S1", "Artist", "Al", 100, artist_browse_id="CH_A")
    store.upsert_track("v2", "S2", "Artist", "Al", 100, artist_browse_id="CH_A")
    store.upsert_track("v3", "S3", "Artist", "Al", 100, artist_browse_id="CH_B")
    assert store.collection.artist_browse_id("Artist") == "CH_A"
    assert store.collection.artist_browse_id("Nobody") is None


def test_facade_delegates(store):
    store.add_saved_album(_album("B1"))                         # legacy store.x() call site
    assert store.saved_album_ids() == {"B1"}
    assert store.collection_albums() == []                     # no playlists → no albums
