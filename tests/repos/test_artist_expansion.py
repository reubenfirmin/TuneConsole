from tests.recommendation_support import seed_publication_subjects
from yt_playlist.core.artists import ensure


def _catalog(store, prefix, title, artist, count):
    # This query owns the real library size and credit index. Provider credit
    # ingestion is a completed lower input, with one shared artist identity.
    seed_publication_subjects(store, [
        {'key': f'{prefix}-{i}', 'title': title, 'artist': artist, 'video_id': f'{prefix}-{i}'}
        for i in range(count)])
    with store.conn:
        subject = ensure(store.conn, artist)
        store.conn.execute("INSERT INTO entry_artists(entry_id,position,artist_id,credit,source) "
            "SELECT e.id,0,?,?,'fixture' FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id "
            'WHERE r.artist=?', (subject, artist, artist))
    return subject


def _legacy(store, wanted):
    return [{"key": r["k"], "title": r["title"], "artist": r["artist"], "album": r["album"] or "",
             "video_id": r["vid"], "thumbnail": r["thumb"], "genre": r["genre"] or ""}
            for r in store.conn.execute(
                "SELECT t.identity_key k,t.title,t.artist,t.album,t.video_id vid,t.thumbnail thumb,t.genre "
                "FROM playable_library_tracks t WHERE t.title<>'' AND EXISTS "
                "(SELECT 1 FROM entry_artists ta WHERE ta.entry_id=t.id AND ta.artist_id=?) "
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
    artist = _catalog(store, 'wanted', 'Wanted', 'Artist', 8)
    before, small, _ = _measured(store, artist)
    _catalog(store, 'other', 'Other', 'Unrelated', 1500)
    after, large, plan = _measured(store, artist)
    assert after == before
    assert large <= small * 2 + 500
    assert any("ix_entry_artist_subject" in step for step in plan)
    assert any("ix_entries_recording" in step for step in plan)
    indexes = [step for step in plan if "ix_entry_artist_subject" in step or "ix_entries_recording" in step]
    print(f"Artist expansion: 8 matches, library 8 to 1508; VM steps {small} to {large}; indexes: {indexes}")


def test_many_requested_artists_deduplicate_across_batches(store):
    credits = [{"name": f"Credit {i}"} for i in range(401)]
    # Put this recording's two credits in different 400-artist query batches.
    # The remaining artists are real lookup subjects, not an oversized credit.
    for credit in credits:
        store.artists.ensure_artist(credit['name'])
    store.tracks.upsert_track("shared", "Collaboration", "Credit 0", "Album", 200,
                             credits=[credits[0], credits[-1]])
    rows = store.rec_query.tracks_by_artists([c["name"] for c in credits])
    assert len(rows) == 1
    assert rows[0]["video_id"] == "shared"
