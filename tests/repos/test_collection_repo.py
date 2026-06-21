"""DAO suite for CollectionRepo (saved-album CRUD + aggregate library views)."""


def _album(browse, **kw):
    return {"browse": browse, "title": kw.get("title", "T"), "artist": kw.get("artist", "A"),
            "year": kw.get("year"), "type": kw.get("type", "Album"), "thumbnail": kw.get("thumbnail")}


def test_saved_album_crud(store):
    store.collection.add_saved_album(_album("B1", title="One", artist="Alpha"))
    store.collection.add_saved_album(_album("B2", title="Two", artist="Beta"))
    assert store.collection.saved_album_ids() == {"B1", "B2"}
    # sorted by artist then title
    assert [a["browse"] for a in store.collection.get_saved_albums()] == ["B1", "B2"]
    store.collection.remove_saved_album("B1")
    assert store.collection.saved_album_ids() == {"B2"}


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
