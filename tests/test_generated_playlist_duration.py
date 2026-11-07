"""Generated playlists must materialize with track times (#26 follow-up). Recommendation tracks
arrive without a duration (the rec pipeline doesn't carry one), so create_generated_playlist
resolves it before saving and applies the Taste Model track-length limit (#44)."""
import json

import pytest

from yt_playlist.library.playlist_generated import create_generated_playlist
from yt_playlist.rec import rec_params
from tests.conftest import FakeClient


def test_generated_playlist_retains_acknowledged_occurrence_handles(store):
    identity = store.accounts.upsert_account('main', 'cred', None, True)

    class ReceiptClient(FakeClient):
        def add_playlist_items(self, playlist, video_ids, **kwargs):
            super().add_playlist_items(playlist, video_ids, **kwargs)
            return {'status': 'STATUS_SUCCEEDED', 'playlistEditResults': [
                {'videoId': video, 'setVideoId': 'owned-' + video} for video in video_ids]}

    result = create_generated_playlist(store, 'Gen', [
        {'video_id': 'original', 'title': 'Song', 'artist': 'Artist', 'duration': 200}],
        ReceiptClient(catalog={'original': {'videoId': 'original'}}), 1, account_id=identity)
    assert result['added_handles'] == {'original': 'owned-original'}
    action = store.actions.get_actions()[0]
    assert json.loads(action.params_json)['added_handles'] == result['added_handles']


@pytest.mark.parametrize("minutes,source,materialize", [
    (20, source, materialize)
    for source in ("supplied", "cached", "fetched") for materialize in (False, True)
] + [(5, "supplied", True), (120, "fetched", False)])
def test_duration_limit_filters_before_remote_writes(store, minutes, source, materialize):
    iid = store.accounts.upsert_account("main", "cred", None, True)
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

    # Successful writes must also be visible to Undo's real readback. Keep the
    # catalog free of duration metadata so it cannot satisfy a lookup for us.
    client = CheckedClient(catalog={video: {"videoId": video} for video in lengths},
                           song_durations=lengths)
    result = create_generated_playlist(store, "Gen", tracks, client, 100,
                                       account_id=iid if materialize else None)
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
    iid = store.accounts.upsert_account("main", "cred", None, True)
    client = FakeClient(song_durations={"mix": 7200})
    with pytest.raises(ValueError, match="maximum track length set in Taste Model"):
        create_generated_playlist(store, "Gen", [{"video_id": "mix", "title": "Concert"}],
                                  client, 100, account_id=iid)
    assert not client.created and not client.added
    assert store.playlists.get_playlists() == []
    assert store.tracks.duration_for_video('mix') == 7200
    assert store.rec_query.tracks_total() == 0


def test_exact_upload_duration_takes_precedence_over_an_alternate_version(store):
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    store.tracks.observe_track_entry('live-version', 'Song', 'Artist', duration_s=9000)
    store.tracks.observe_track_entry('single', 'Song', 'Artist', duration_s=200)
    client = FakeClient(catalog={'single': {'videoId': 'single'}})
    result = create_generated_playlist(store, 'Gen', [
        {'video_id': 'single', 'title': 'Song', 'artist': 'Artist'}], client, 100, account_id=iid)
    assert result['added'] == 1
    assert store.charts.playlist_tracks_detail(result['pid'])[0]['duration'] == 200


def test_failed_duration_lookup_still_allows_generation(store, monkeypatch):
    from yt_playlist.library import playlist_catalog

    iid = store.accounts.upsert_account("main", "cred", None, True)
    calls = []

    class UnavailableClient(FakeClient):
        def get_song(self, video_id):
            calls.append(video_id)
            raise RuntimeError("duration unavailable")

    monkeypatch.setattr(playlist_catalog, "with_retry", lambda fn: fn())
    client = UnavailableClient(catalog={'unknown': {'videoId': 'unknown'}})
    result = create_generated_playlist(store, "Gen", [{"video_id": "unknown"}], client, 100,
                                       account_id=iid)
    assert result["added"] == 1
    assert calls == ["unknown"]
    assert store.charts.playlist_tracks_detail(result["pid"])[0]["duration"] is None


@pytest.mark.parametrize('duration,resume', [(200, False), (9000, False), (200, True)])
def test_learned_duration_during_pause_filters_without_writing(store, duration, resume):
    account = store.accounts.upsert_account('main', 'cred', None, True)

    class DuringPause(FakeClient):
        def add_playlist_items(self, *args, **kwargs):
            if resume:
                with store._lock:
                    store.conn.execute('PRAGMA query_only=OFF')
                    store.activity_paused = False
            return super().add_playlist_items(*args, **kwargs)

    client = DuringPause(song_durations={'unknown': duration})
    with store._lock:
        store.activity_paused = True
        store.conn.execute('PRAGMA query_only=ON')
    try:
        if duration > 1200:
            with pytest.raises(ValueError, match='maximum track length'):
                create_generated_playlist(store, 'Paused', [{'video_id': 'unknown'}], client, 1, account)
            assert not client.created and not client.added
        else:
            result = create_generated_playlist(store, 'Paused', [{'video_id': 'unknown'}], client, 1, account)
            assert result['added'] == 1 and result['pid'] is None
            assert result['tracks'][0]['duration'] == duration
        assert store.tracks.duration_for_video('unknown') is None
        assert store.playlists.get_playlists() == []
        assert store.actions.get_actions() == []
    finally:
        with store._lock:
            store.conn.execute('PRAGMA query_only=OFF')
            store.activity_paused = False
