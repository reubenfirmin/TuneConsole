import sqlite3
from yt_playlist.core.store import Store


def test_fresh_schema_has_audio_columns():
    s = Store(":memory:")
    s.init_schema()
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(tracks)")}
    assert {"bpm", "energy", "danceability", "mb_recording_id",
            "music_key", "music_scale", "mood_happy", "mood_sad", "mood_relaxed",
            "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
            "popularity", "gain", "label"} <= cols


def test_migration_adds_audio_columns_to_old_db(tmp_path):
    # simulate a pre-existing DB without the new columns
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, video_id TEXT, "
                "title TEXT, artist TEXT, album TEXT, duration_s INTEGER, "
                "identity_key TEXT NOT NULL, genre TEXT, mb_year TEXT)")
    raw.commit()
    raw.close()
    s = Store(str(db))
    s.init_schema()
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(tracks)")}
    assert {"bpm", "energy", "danceability", "mb_recording_id",
            "music_key", "music_scale", "mood_happy", "mood_sad", "mood_relaxed",
            "mood_acoustic", "instrumental", "loudness", "dynamic_complexity",
            "popularity", "gain", "label"} <= cols


def _seed_playlist(store, titles):
    iid = store.upsert_identity("main", "cred", None, True)
    tids = [store.upsert_track(f"v{i}", t, "Artist", "Alb", 200) for i, t in enumerate(titles)]
    pid = store.upsert_playlist(iid, "PL1", "Mix", len(tids), "h", 1000.0)
    store.set_playlist_tracks(pid, tids)
    return pid, tids


def test_set_and_get_track_audio_fill_only(store):
    pid, (t1,) = _seed_playlist(store, ["A"])
    store.set_track_audio(t1, bpm=128.0, energy=0.7, danceability=0.9)
    assert store.get_track_audio(t1) == (128.0, 0.7, 0.9)
    # fill-only: a second call must NOT overwrite existing values
    store.set_track_audio(t1, bpm=999.0, energy=0.1, danceability=0.1)
    assert store.get_track_audio(t1) == (128.0, 0.7, 0.9)
    # but it fills a still-null column
    _, (t2,) = _seed_playlist(store, ["B"])
    store.set_track_audio(t2, bpm=120.0)
    assert store.get_track_audio(t2) == (120.0, None, None)
    store.set_track_audio(t2, energy=0.5)
    assert store.get_track_audio(t2) == (120.0, 0.5, None)


def test_set_track_audio_fills_new_fields(store):
    pid, (t1,) = _seed_playlist(store, ["A"])
    store.set_track_audio(t1, bpm=128.0, music_key="A", music_scale="minor",
                          mood_happy=0.7, mood_sad=0.2, mood_relaxed=0.6, mood_acoustic=0.3,
                          instrumental=0.1, loudness=0.9, dynamic_complexity=3.2,
                          popularity=856376, gain=-7.0, label="Because Music")
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
    # fill-only: a second call must not overwrite an already-set new field
    store.set_track_audio(t1, music_key="Z", label="Other")
    row2 = store.conn.execute("SELECT music_key, label FROM tracks WHERE id=?", (t1,)).fetchone()
    assert row2["music_key"] == "A"
    assert row2["label"] == "Because Music"


def test_set_track_mbid_fill_only(store):
    pid, (t1,) = _seed_playlist(store, ["A"])
    store.set_track_mbid(t1, "mbid-1")
    store.set_track_mbid(t1, "mbid-2")
    rows = list(store.tracks_missing_audio(pid))
    assert rows[0]["mb_recording_id"] == "mbid-1"


def test_tracks_missing_audio_lists_incomplete_tracks(store):
    pid, (t1, t2) = _seed_playlist(store, ["A", "B"])
    store.set_track_audio(t1, bpm=128.0, energy=0.7, danceability=0.9)  # fully populated
    missing = [r["id"] for r in store.tracks_missing_audio(pid)]
    assert missing == [t2]
