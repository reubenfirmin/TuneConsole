"""Generated playlists must materialize with track times (#26 follow-up). Recommendation tracks
arrive without a duration (the rec pipeline doesn't carry one), so create_generated_playlist
resolves it server-side: reuse a time we already know for the song, else best-effort fetch."""
from yt_playlist.library.executor import create_generated_playlist
from tests.conftest import FakeClient


def test_generated_playlist_reuses_known_duration(store):
    # A library track already has a time under one videoId; a recommendation surfaces the same song
    # under a different videoId with no duration. We reuse the known time. No network call needed.
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("vOLD", "Song", "Artist", "Alb", 240, 1)   # known time, different videoId
    fc = FakeClient()                                             # get_song would return no length
    res = create_generated_playlist(
        store, "Gen", [{"video_id": "vNEW", "title": "Song", "artist": "Artist"}],
        fc, 1.0, identity_id=iid)
    row = next(t for t in store.playlist_tracks_detail(res["pid"]) if t["video_id"] == "vNEW")
    assert row["duration"] == 240


def test_generated_playlist_fetches_missing_duration_for_fresh_track(store):
    # A 'fresh song' not in the library and with no known time gets its duration fetched at
    # materialization, so the generated playlist row isn't left blank.
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient(song_durations={"v1": 222})
    res = create_generated_playlist(
        store, "Gen", [{"video_id": "v1", "title": "New Tune", "artist": "Nobody"}],
        fc, 1.0, identity_id=iid)
    row = next(t for t in store.playlist_tracks_detail(res["pid"]) if t["video_id"] == "v1")
    assert row["duration"] == 222


def test_generated_playlist_survives_unfetchable_duration(store):
    # If the time is unknown everywhere and the fetch comes back empty, the track still lands.
    # A missing duration must never block playlist creation.
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient()                                            # no known time, get_song empty
    res = create_generated_playlist(
        store, "Gen", [{"video_id": "v9", "title": "Mystery", "artist": "Ghost"}],
        fc, 1.0, identity_id=iid)
    row = next(t for t in store.playlist_tracks_detail(res["pid"]) if t["video_id"] == "v9")
    assert row["duration"] is None and res["added"] == 1
