from tests.conftest import seed_catalog_track


def _legacy(store, wanted):
    return [{"key": r["k"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
             "video_id": r["vid"], "thumbnail": r["thumb"], "genre": r["genre"] or ""}
            for r in store.conn.execute(
                "SELECT t.identity_key k,t.title,t.artist,t.album,t.video_id vid,t.thumbnail thumb,t.genre "
                "FROM playable_library_tracks t WHERE t.title<>'' AND EXISTS "
                "(SELECT 1 FROM track_artists ta WHERE ta.entry_id=t.id AND ta.artist_id=?) "
                "ORDER BY t.identity_key", (wanted,))]


def test_artist_expansion_preserves_playable_representative_and_credits(store):
    first = store.tracks.upsert_track("a", "Song", "First", "Old", 200, False)
    second = store.tracks.upsert_track("b", "Song", "First", "Chosen", 210, True, thumbnail="cover",
                                      credits=[{"name": "First"}, {"name": "Guest"}])
    recording = store.tracks.catalog_subject(first)["recording_id"]
    store.tracks.associate_youtube_item("b", recording, source="verified", evidence={"same": True}, now=1)
    store.tracks.set_track_genre(second, "Rock")
    store.tracks.upsert_track(None, "Unresolved", "Guest", None, 200)
    store.tracks.upsert_track("unavailable", "Missing", "Guest", None, 200, False)
    guest = store.artists.resolve("Guest")
    expected = _legacy(store, guest)
    assert len(expected) == 1
    assert expected[0]["video_id"] == "b"
    assert store.rec_query.tracks_by_artists(["Guest", "First", "unknown"]) == expected
    assert store.rec_query.tracks_by_artists([guest]) == expected
    assert store.rec_query.tracks_by_artists([]) == []
    assert store.rec_query.tracks_by_artists(["unknown"]) == []


def _measured(store, artist):
    steps, statements = [0], []
    with store.read_snapshot() as snapshot:
        conn = snapshot.store.conn
        def progress():
            steps[0] += 100
            return 0
        conn.set_progress_handler(progress, 100)
        conn.set_trace_callback(statements.append)
        try:
            rows = snapshot.store.rec_query.tracks_by_artists([artist])
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_trace_callback(None)
        query = next(s for s in statements if s.startswith("WITH requested"))
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + query)]
    return rows, steps[0], plan


def test_artist_expansion_work_is_bounded_by_requested_catalog(store):
    for i in range(8):
        seed_catalog_track(store, f"wanted-{i}", "Wanted", "Artist", video_id=f"wanted-{i}")
    artist = store.artists.resolve("Artist")
    before, small, _ = _measured(store, artist)
    for i in range(1500):
        seed_catalog_track(store, f"other-{i}", "Other", "Unrelated", video_id=f"other-{i}")
    after, large, plan = _measured(store, artist)
    assert after == before
    assert large <= small * 2 + 500
    assert any("ix_track_artist_subject" in step for step in plan)
    assert any("ix_entries_recording" in step for step in plan)
    indexes = [step for step in plan if "ix_track_artist_subject" in step or "ix_entries_recording" in step]
    print(f"Artist expansion: 8 matches, library 8 to 1508; VM steps {small} to {large}; indexes: {indexes}")


def test_many_requested_artists_deduplicate_across_batches(store):
    credits = [{"name": f"Credit {i}"} for i in range(401)]
    store.tracks.upsert_track("shared", "Collaboration", "Credit 0", "Album", 200, credits=credits)
    rows = store.rec_query.tracks_by_artists([c["name"] for c in credits])
    assert len(rows) == 1
    assert rows[0]["video_id"] == "shared"
