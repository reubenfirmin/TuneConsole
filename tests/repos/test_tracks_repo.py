"""DAO suite for TrackRepo (track rows + genre/year enrichment)."""


def test_upsert_dedupes_by_identity_and_backfills(store):
    a = store.tracks.upsert_track("v1", "Song", "Artist", "Album", 100)
    b = store.tracks.upsert_track("v1", "Song", "Artist", "Album", 100, thumbnail="t.jpg")
    assert a == b                                          # same identity_key + video_id → same row
    assert store.conn.execute("SELECT thumbnail FROM tracks WHERE id=?", (a,)).fetchone()["thumbnail"] == "t.jpg"


def test_manual_genre_year_overrides(store):
    t = store.tracks.upsert_track("v1", "S", "A", "Al", 100)
    store.tracks.set_track_genre(t, "Techno")
    store.tracks.set_track_year(t, "1999")
    assert store.tracks.get_track_enrichment(t) == ("Techno", "1999")
    store.tracks.set_track_genre(t, "")                    # blank clears
    assert store.tracks.get_track_enrichment(t) == ("", "1999")


def test_set_title_artist_and_reset(store):
    t = store.tracks.upsert_track("v1", "Bad - Title (junk)", "Bad Artist", "Al", 100)
    store.tracks.set_track_title(t, "Good Title")
    store.tracks.set_track_artist(t, "Good Artist")
    row = store.conn.execute("SELECT title, artist FROM tracks WHERE id=?", (t,)).fetchone()
    assert (row["title"], row["artist"]) == ("Good Title", "Good Artist")
    store.tracks.reset_track_title(t)
    store.tracks.reset_track_artist(t)
    row = store.conn.execute("SELECT title, artist FROM tracks WHERE id=?", (t,)).fetchone()
    assert (row["title"], row["artist"]) == ("Bad - Title (junk)", "Bad Artist")


def test_set_title_rejects_blank(store):
    t = store.tracks.upsert_track("v1", "Keep Me", "A", "Al", 100)
    store.tracks.set_track_title(t, "   ")
    assert store.conn.execute("SELECT title FROM tracks WHERE id=?", (t,)).fetchone()["title"] == "Keep Me"


def test_enrichment_is_fill_only(store):
    t = store.tracks.upsert_track("v1", "S", "A", "Al", 100)
    store.tracks.set_track_enrichment(t, "House", "2001")
    store.tracks.set_track_enrichment(t, "Trance", "2002")  # must NOT overwrite existing values
    assert store.tracks.get_track_enrichment(t) == ("House", "2001")


def test_missing_genre_and_year_queries(store):
    iid = store.upsert_identity("me", "c", None, True)
    p = store.upsert_playlist(iid, "P", "P", 0, "h", 0.0)
    t1 = store.tracks.upsert_track("v1", "S1", "A", "Al", 100)
    t2 = store.tracks.upsert_track("v2", "S2", "A", "Al", 100)
    store.set_playlist_tracks(p, [t1, t2])
    store.tracks.set_track_enrichment(t1, "House", "2001")  # t1 fully tagged
    assert [r["id"] for r in store.tracks.tracks_missing_genre(p)] == [t2]
    assert [r["id"] for r in store.tracks.tracks_to_enrich(p)] == [t2]


def test_track_ids_for_videos_latest_wins(store):
    t = store.tracks.upsert_track("v1", "S", "A", "Al", 100)
    assert store.tracks.track_ids_for_videos(["v1", "nope"]) == {"v1": t}


def test_facade_delegates(store):
    t = store.upsert_track("v1", "S", "A", "Al", 100)       # legacy store.x() call site
    store.set_track_year(t, "2020")
    assert store.get_track_enrichment(t) == ("", "2020")
