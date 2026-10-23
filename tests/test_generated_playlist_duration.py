"""Generated playlists must materialize with track times (#26 follow-up). Recommendation tracks
arrive without a duration (the rec pipeline doesn't carry one), so create_generated_playlist
resolves it before saving and applies the Taste Model track-length limit (#44)."""
import pytest

from yt_playlist.library.executor import create_generated_playlist
from yt_playlist.rec import rec_params
from tests.conftest import FakeClient


def test_generated_playlist_reuses_known_duration(store):
    # A library track already has a time under one videoId; a recommendation surfaces the same song
    # under a different videoId with no duration. We reuse the known time. No network call needed.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.tracks.upsert_track("vOLD", "Song", "Artist", "Alb", 240, 1)   # known time, different videoId
    fc = FakeClient()                                             # get_song would return no length
    res = create_generated_playlist(
        store, "Gen", [{"video_id": "vNEW", "title": "Song", "artist": "Artist"}],
        fc, 1.0, identity_id=iid)
    row = next(t for t in store.charts.playlist_tracks_detail(res["pid"]) if t["video_id"] == "vNEW")
    assert row["duration"] == 240


def test_generated_playlist_fetches_missing_duration_for_fresh_track(store):
    # A 'fresh song' not in the library and with no known time gets its duration fetched at
    # materialization, so the generated playlist row isn't left blank.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fc = FakeClient(song_durations={"v1": 222})
    res = create_generated_playlist(
        store, "Gen", [{"video_id": "v1", "title": "New Tune", "artist": "Nobody"}],
        fc, 1.0, identity_id=iid)
    row = next(t for t in store.charts.playlist_tracks_detail(res["pid"]) if t["video_id"] == "v1")
    assert row["duration"] == 222


def test_generated_playlist_survives_unfetchable_duration(store):
    # If the time is unknown everywhere and the fetch comes back empty, the track still lands.
    # A missing duration must never block playlist creation.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fc = FakeClient()                                            # no known time, get_song empty
    res = create_generated_playlist(
        store, "Gen", [{"video_id": "v9", "title": "Mystery", "artist": "Ghost"}],
        fc, 1.0, identity_id=iid)
    row = next(t for t in store.charts.playlist_tracks_detail(res["pid"]) if t["video_id"] == "v9")
    assert row["duration"] is None and res["added"] == 1


@pytest.mark.parametrize("minutes", [5, 20, 120])
@pytest.mark.parametrize("source", ["supplied", "cached", "fetched"])
@pytest.mark.parametrize("materialize", [False, True])
def test_duration_limit_filters_before_remote_writes(store, minutes, source, materialize):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    if minutes != 20:
        rec_params.set_param(store, "max_track_minutes", minutes)
    lengths = {"short": 200, "boundary": minutes * 60, "long": minutes * 60 + 1, "unknown": None}
    tracks = [{"video_id": video, "title": video, "artist": "Artist"} for video in lengths]
    if source == "supplied":
        tracks = [{**t, "duration": lengths[t["video_id"]]} for t in tracks]
    elif source == "cached":
        for video, duration in lengths.items():
            if duration is not None:
                store.tracks.upsert_track("old_" + video, video, "Artist", None, duration)
    tracks.append(dict(tracks[0]))    # deduplication must happen before lookups
    original = [dict(t) for t in tracks]
    lookups = []

    class CheckedClient(FakeClient):
        def get_song(self, video_id):
            assert not self.created and not self.added
            lookups.append(video_id)
            return super().get_song(video_id)

    client = CheckedClient(song_durations=lengths)
    result = create_generated_playlist(store, "Gen", tracks, client, 100,
                                       identity_id=iid if materialize else None)
    assert client.added == [(result["new_ytm"], ["short", "boundary", "unknown"])]
    assert result["added"] == 3 and result["skipped"] == 1
    assert lookups == (list(lengths) if source == "fetched" else ["unknown"])
    assert tracks == original
    if materialize:
        rows = store.charts.playlist_tracks_detail(result["pid"])
        assert [(r["video_id"], r["duration"]) for r in rows] == [
            ("short", 200), ("boundary", minutes * 60), ("unknown", None)]
    else:
        assert result["pid"] is None


def test_all_overlong_tracks_do_not_create_an_empty_remote_playlist(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    client = FakeClient(song_durations={"mix": 7200})
    with pytest.raises(ValueError, match="maximum track length set in Taste Model"):
        create_generated_playlist(store, "Gen", [{"video_id": "mix", "title": "Concert"}],
                                  client, 100, identity_id=iid)
    assert not client.created and not client.added
    assert store.playlists.get_playlists() == []


def test_failed_duration_lookup_still_allows_generation(store, monkeypatch):
    from yt_playlist.library import executor

    iid = store.identities.upsert_identity("main", "cred", None, True)
    calls = []

    class UnavailableClient(FakeClient):
        def get_song(self, video_id):
            calls.append(video_id)
            raise RuntimeError("duration unavailable")

    monkeypatch.setattr(executor, "with_retry", lambda fn: fn())
    client = UnavailableClient()
    result = create_generated_playlist(store, "Gen", [{"video_id": "unknown"}], client, 100,
                                       identity_id=iid)
    assert result["added"] == 1
    assert calls == ["unknown"]
    assert store.charts.playlist_tracks_detail(result["pid"])[0]["duration"] is None
