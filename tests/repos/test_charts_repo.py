"""DAO suite for ChartsRepo (play-history stats for charts / artist / playlist pages)."""


def _seed(store):
    iid = store.upsert_identity("me", "c", None, True)
    a = store.upsert_track("v1", "Alpha", "Artist A", "Album A", 200)
    b = store.upsert_track("v2", "Beta", "Artist B", "Album B", 200)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 0.0)
    pl = store.upsert_playlist(iid, "P1", "Mix", 0, "h", 0.0)
    store.set_playlist_tracks(pl, [a, b])
    store.set_playlist_tracks(lm, [a])                     # Alpha is liked (in LM)
    ka = store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (a,)).fetchone()["k"]
    kb = store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (b,)).fetchone()["k"]
    store.add_history_snapshot(iid, 1000.0, [ka, ka, kb])  # Alpha 2 plays, Beta 1
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


def test_facade_delegates(store):
    _seed(store)
    assert store.top_tracks()[0]["title"] == "Alpha"       # legacy store.x() call site
    assert store.artist_songs("Artist B")[0]["title"] == "Beta"


def test_playlist_detail_flags_edited_title_artist(store):
    iid = store.upsert_identity("me", "c", None, True)
    p = store.upsert_playlist(iid, "P", "P", 0, "h", 0.0)
    t = store.upsert_track("v1", "Orig Title", "Orig Artist", "Al", 100)
    store.set_playlist_tracks(p, [t])
    d = store.playlist_tracks_detail(p)[0]
    assert d["title_edited"] is False and d["artist_edited"] is False
    store.set_track_title(t, "New Title")
    d = store.playlist_tracks_detail(p)[0]
    assert d["title_edited"] is True and d["artist_edited"] is False
