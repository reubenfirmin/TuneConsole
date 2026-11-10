from yt_playlist.core.store import Store


def test_fresh_schema_has_audio_columns():
    s = Store(":memory:")
    s.init_schema()
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(tracks)")}
    assert {"bpm", "energy", "danceability", "mb_recording_id",
            "music_key", "music_scale", "mood_happy", "mood_sad", "mood_relaxed",
            "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
            "popularity", "gain", "label"} <= cols


def _seed_playlist(store, titles):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    tids = [store.tracks.upsert_track(f"v:{t}", t, "Artist", "Alb", 200) for t in titles]
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", len(tids), "h", 1000.0)
    store.playlists.set_playlist_tracks(pid, tids)
    return pid, tids


def test_provider_audio_priority_fills_gaps_without_overwriting(store):
    pid, (t1,) = _seed_playlist(store, ["A"])
    providers = ('deezer', 'acousticbrainz')
    store.enrichment.apply_provider_fields(t1, 'deezer', {'bpm': 128.0, 'energy': .7, 'danceability': .9}, providers=providers)
    assert store.tracks.get_track_audio(t1) == (128.0, 0.7, 0.9)
    # A lower-priority provider fills gaps without replacing the preferred source.
    store.enrichment.apply_provider_fields(t1, 'acousticbrainz', {'bpm': 999.0, 'energy': .1, 'danceability': .1}, providers=providers)
    assert store.tracks.get_track_audio(t1) == (128.0, 0.7, 0.9)
    # The selected source can refresh its own value; priority is not a permanent freeze.
    store.enrichment.apply_provider_fields(t1, 'deezer', {'bpm': 129.0}, providers=providers)
    assert store.tracks.get_track_audio(t1) == (129.0, .7, .9)
    # but it fills a still-null column
    _, (t2,) = _seed_playlist(store, ["B"])
    store.enrichment.apply_provider_fields(t2, 'deezer', {'bpm': 120.0}, providers=providers)
    assert store.tracks.get_track_audio(t2) == (120.0, None, None)
    store.enrichment.apply_provider_fields(t2, 'acousticbrainz', {'energy': .5}, providers=providers)
    assert store.tracks.get_track_audio(t2) == (120.0, 0.5, None)


def test_provider_audio_fills_extended_fields(store):
    pid, (t1,) = _seed_playlist(store, ["A"])
    providers = ('deezer', 'acousticbrainz')
    store.enrichment.apply_provider_fields(t1, 'deezer', dict(bpm=128.0, music_key="A", music_scale="minor",
                          mood_happy=0.7, mood_sad=0.2, mood_relaxed=0.6, mood_acoustic=0.3,
                          instrumental=0.1, loudness=0.9, dynamic_complexity=3.2,
                          popularity=856376, gain=-7.0, label="Because Music"), providers=providers)
    row = store.conn.execute(
        "SELECT music_key, music_scale, mood_happy, mood_sad, mood_relaxed, mood_acoustic, "
        "instrumental, loudness, dynamic_complexity, popularity, gain, label "
        "FROM tracks WHERE id=?", (t1,)).fetchone()
    assert row["music_key"] == "A"
    assert row["music_scale"] == "minor"
    assert row["mood_happy"] == 0.7
    assert row["instrumental"] == 0.1
    assert row["popularity"] == 856376
    assert row["gain"] == -7.0
    assert row["label"] == "Because Music"
    store.enrichment.apply_provider_fields(t1, 'acousticbrainz', {'music_key': 'Z', 'label': 'Other'}, providers=providers)
    row2 = store.conn.execute("SELECT music_key, label FROM tracks WHERE id=?", (t1,)).fetchone()
    assert row2["music_key"] == "A"
    assert row2["label"] == "Because Music"


def test_preferred_provider_mbid_reaches_waterfall_reader(store):
    pid, (t1,) = _seed_playlist(store, ["A"])
    providers = ('musicbrainz', 'acousticbrainz')
    store.enrichment.apply_provider_fields(t1, 'musicbrainz', {'mb_recording_id': 'mbid-1'}, providers=providers)
    store.enrichment.apply_provider_fields(t1, 'acousticbrainz', {'mb_recording_id': 'mbid-2'}, providers=providers)
    assert store.tracks.track_for_waterfall(t1)['mb_recording_id'] == 'mbid-1'


def test_waterfall_lists_tracks_with_missing_audio(store):
    pid, (t1, t2) = _seed_playlist(store, ["A", "B"])
    for entry in (t1, t2):
        store.enrichment.apply_provider_fields(entry, 'musicbrainz', {'genre': 'House', 'year': '2001'})
        store.tracks.set_track_art(entry, 'https://example.com/cover.jpg')
    store.enrichment.apply_provider_fields(t1, 'deezer', {'bpm': 128.0, 'energy': .7, 'danceability': .9})
    missing = [r["id"] for r in store.tracks.tracks_for_waterfall(pid)]
    assert missing == [t2]
