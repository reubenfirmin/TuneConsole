"""DAO suite for ChartsRepo (play-history stats for charts / artist / playlist pages)."""


def _seed(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    a = store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 200)
    b = store.tracks.upsert_track("v2", "Beta", "Artist B", "Album B", 200)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 0.0)
    pl = store.playlists.upsert_playlist(iid, "P1", "Mix", 0, "h", 0.0)
    store.playlists.set_playlist_tracks(pl, [a, b])
    store.playlists.set_playlist_tracks(lm, [a])                     # Alpha is liked (in LM)
    ka = store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (a,)).fetchone()["k"]
    kb = store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (b,)).fetchone()["k"]
    store.history.add_history_snapshot(iid, 1000.0, [ka, ka, kb])  # Alpha 2 plays, Beta 1
    return iid, pl


def test_top_tracks_ranks_by_plays(store):
    _seed(store)
    top = store.charts.top_tracks()
    assert [t["title"] for t in top] == ["Alpha", "Beta"]
    assert top[0]["plays"] == 2 and top[1]["plays"] == 1


def test_top_artists_sums_plays(store):
    _seed(store)
    top = store.charts.top_artists()
    assert top[0]["artist"] == "Artist A" and top[0]["plays"] == 2


def test_ticker_artwork_uses_saved_album_cover_and_ignores_empty_thumbnails(store):
    store.tracks.upsert_track("empty", "Empty", "Artist A", "Album A", 180, thumbnail="")
    store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 180,
                       album_browse_id="MPRE_a", thumbnail="/track-art.png")
    store.tracks.upsert_track("v2", "Beta", "Artist B", "Album B", 180)
    store.collection.add_saved_album({"browse": "MPRE_a", "title": "Album A", "artist": "Artist A",
                           "thumbnail": "/album-art.png"})

    assert store.charts.ticker_thumbnails("artist") == {"Artist A": "/track-art.png"}
    assert store.charts.ticker_thumbnails("album") == {"Album A": "/album-art.png"}

    store.collection.remove_saved_album("MPRE_a")
    assert store.charts.ticker_thumbnails("album") == {"Album A": "/track-art.png"}


def test_playlist_ticker_artwork_prefers_cover_and_falls_back_to_track_art(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    track = store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 180, thumbnail="/track.png")
    cover = store.playlists.upsert_playlist(iid, "P1", "With cover", 1, "h", 0.0, thumbnail="/playlist.png")
    fallback = store.playlists.upsert_playlist(iid, "P2", "Track art", 1, "h", 0.0)
    store.playlists.upsert_playlist(iid, "P3", "No art", 0, "h", 0.0)
    store.playlists.set_playlist_tracks(cover, [track])
    store.playlists.set_playlist_tracks(fallback, [track])

    assert store.charts.ticker_thumbnails("playlist") == {"With cover": "/playlist.png", "Track art": "/track.png"}


def test_playlist_tracks_detail_marks_liked(store):
    _, pl = _seed(store)
    detail = store.charts.playlist_tracks_detail(pl)
    by_title = {d["title"]: d for d in detail}
    assert by_title["Alpha"]["liked"] is True and by_title["Beta"]["liked"] is False
    assert by_title["Alpha"]["plays"] == 2


def test_listen_stats_and_artist_songs(store):
    _, pl = _seed(store)
    stats = store.charts.get_playlist_listen_stats()
    assert stats[pl][1] == 3                                # 3 history-item hits across the playlist
    songs = store.charts.artist_songs("Artist A")
    assert len(songs) == 1 and songs[0]["title"] == "Alpha" and songs[0]["liked"] is True
    assert [p["ytm"] for p in songs[0]["playlists"]] == sorted(["LM", "P1"], key=str.lower) or \
           {p["ytm"] for p in songs[0]["playlists"]} == {"LM", "P1"}


def test_artist_songs_can_disambiguate_same_name_by_browse_id(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    first = store.tracks.upsert_track("v1", "First act", "Shared Name", "One", 180,
                               artist_browse_id="UC_first")
    second = store.tracks.upsert_track("v2", "Second act", "Shared Name", "Two", 180,
                                artist_browse_id="UC_second")
    store.tracks.upsert_track("v3", "Unknown act", "Shared Name", "Three", 180)
    playlist = store.playlists.upsert_playlist(iid, "P1", "Mix", 2, "h", 0.0)
    store.playlists.set_playlist_tracks(playlist, [first, second])

    assert [s["title"] for s in store.charts.artist_songs("Shared Name", "UC_first")] == ["First act"]


def test_artist_songs_includes_album_tracks_missing_an_unambiguous_artist_id(store):
    store.tracks.upsert_track("v1", "One", "Artist", "First", 180, artist_browse_id="UC_artist")
    store.collection.add_saved_album({"browse": "B1", "title": "Second", "artist": "Artist"})
    store.tracks.upsert_track("v2", "Two", "Artist", "Second", 180, album_browse_id="B1")

    songs = store.charts.artist_songs("Artist", "UC_artist")
    assert {s["title"] for s in songs} == {"One", "Two"}
    assert all(s["playlists"] == [] for s in songs)
    assert store.charts.artist_songs("Artist", "UC_unrelated") == []


def test_store_binding(store):
    _seed(store)
    assert store.charts.top_tracks()[0]["title"] == "Alpha"
    assert store.charts.artist_songs("Artist B")[0]["title"] == "Beta"


def test_playlist_detail_flags_edited_title_artist(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    p = store.playlists.upsert_playlist(iid, "P", "P", 0, "h", 0.0)
    t = store.tracks.upsert_track("v1", "Orig Title", "Orig Artist", "Al", 100)
    store.playlists.set_playlist_tracks(p, [t])
    d = store.charts.playlist_tracks_detail(p)[0]
    assert d["title_edited"] is False and d["artist_edited"] is False
    store.tracks.set_track_title(t, "New Title")
    d = store.charts.playlist_tracks_detail(p)[0]
    assert d["title_edited"] is True and d["artist_edited"] is False
