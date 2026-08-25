import io
import json
import zipfile

import pytest

from yt_playlist.core.store import Store
from yt_playlist.library.spotify_data import (SpotifyDataFormatError, import_spotify_data,
                                               load_streaming_history)


def _row(title="Song", artist="Artist", ts="2024-01-01T10:00:00Z", uri="spotify:track:abc"):
    return {"ts": ts, "master_metadata_track_name": title,
            "master_metadata_album_artist_name": artist, "spotify_track_uri": uri,
            "ms_played": 180000}


def _zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, rows in files.items():
            archive.writestr(name, json.dumps(rows))
    return out.getvalue()


def test_zip_loads_every_audio_history_file_and_ignores_video_and_podcasts():
    raw = _zip({
        "Spotify Extended Streaming History/Streaming_History_Audio_2023.json": [_row(ts="2023-01-01T00:00:00Z")],
        "Spotify Extended Streaming History/Streaming_History_Audio_2024.json": [
            _row(ts="2024-01-01T00:00:00Z"),
            {"ts": "2024-02-01T00:00:00Z", "episode_name": "A podcast",
             "spotify_episode_uri": "spotify:episode:x"}],
        "Spotify Extended Streaming History/Streaming_History_Video_2024.json": [_row(title="Video")],
    })
    rows, skipped = load_streaming_history(raw)
    assert [row["ts"] for row in rows] == sorted(row["ts"] for row in rows)
    assert len(rows) == 2 and skipped == 1


def test_plain_audio_json_is_supported_and_bad_upload_is_rejected():
    assert len(load_streaming_history(json.dumps([_row()]))[0]) == 1
    with pytest.raises(SpotifyDataFormatError):
        load_streaming_history(b"not json")
    with pytest.raises(SpotifyDataFormatError):
        load_streaming_history(_zip({"unrelated.json": []}))


def test_import_matches_library_is_idempotent_and_marks_event_source():
    store = Store(":memory:"); store.init_schema()
    store.upsert_identity("main", "bridge", None, True)
    store.upsert_track("yt-song", "Song", "Artist", None, 180)
    raw = json.dumps([_row(), _row("Unknown", "Elsewhere", "2024-01-02T10:00:00Z")])
    report = import_spotify_data(store, raw)
    assert report["matched"] == 1 and report["unmatched"] == 1
    assert report["plays_added"] == 1 and report["events_added"] == 1
    event = store.conn.execute("SELECT video_id, source FROM play_events").fetchone()
    assert tuple(event) == (None, "spotify")
    again = import_spotify_data(store, raw)
    assert again["plays_added"] == 0 and again["events_added"] == 0


def test_no_identity_returns_error():
    store = Store(":memory:"); store.init_schema()
    assert import_spotify_data(store, json.dumps([])) == {"error": "no identity configured"}
