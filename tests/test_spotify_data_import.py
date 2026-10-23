import io
import json
import zipfile

import pytest

from yt_playlist.core.store import Store
from yt_playlist.library.spotify_data import (SpotifyDataFormatError, import_spotify_data,
                                               load_streaming_history, prepare_streaming_history)


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
    store.identities.upsert_identity("main", "bridge", None, True)
    store.tracks.upsert_track("yt-song", "Song", "Artist", None, 180)
    raw = json.dumps([_row(), _row("Unknown", "Elsewhere", "2024-01-02T10:00:00Z")])
    report = import_spotify_data(store, raw)
    assert report["matched"] == 1 and report["unmatched"] == 1
    assert report["plays_added"] == 1 and report["events_added"] == 1
    event = store.conn.execute("SELECT video_id, source FROM play_events").fetchone()
    assert tuple(event) == (None, "spotify_export")
    again = import_spotify_data(store, raw)
    assert again["plays_added"] == 0 and again["events_added"] == 0


def test_no_identity_returns_error():
    store = Store(":memory:"); store.init_schema()
    assert import_spotify_data(store, json.dumps([])) == {"error": "no identity configured"}


@pytest.mark.parametrize('main_brand,other_brand,main_first', [
    (None, None, True), (None, None, False), ('brand', None, False),
    ('brand', 'other-brand', True), ('brand', None, True),
])
def test_local_export_uses_configured_main_identity_instead_of_live_player_account(
        store, main_brand, other_brand, main_first):
    rows = [('main', main_brand, True), ('other', other_brand, False)]
    if not main_first:
        rows.reverse()
    ids = {label:store.identities.upsert_identity(label, 'bridge', brand, main)
           for label,brand,main in rows}
    store.tracks.upsert_track('yt', 'Song', 'Artist', None, 180)
    report = import_spotify_data(store, json.dumps([_row()]))
    assert report['identity_id'] == ids['main']
    assert store.conn.execute('SELECT identity_id FROM play_events').fetchone()[0] == ids['main']
    assert store.conn.execute('SELECT identity_id FROM spotify_history_listens').fetchone()[0] == ids['main']


def test_local_export_works_when_the_only_identity_is_a_brand_account(store):
    iid = store.identities.upsert_identity('My music', 'bridge', 'brand', True)
    store.tracks.upsert_track('yt', 'Song', 'Artist', None, 180)
    assert import_spotify_data(store, json.dumps([_row()]))['identity_id'] == iid


@pytest.mark.parametrize('field,value', [
    ('master_metadata_track_name', 42), ('master_metadata_album_artist_name', ['Artist']),
    ('spotify_track_uri', {}), ('spotify_track_uri', 'spotify:episode:abc'),
    ('spotify_track_uri', 'spotify:track:'), ('ts', 123), ('ts', '2024-01-01T10:00:00'),
    ('ts', 'invalid'), ('ts','0001-01-01T00:00:00+14:00'),
    ('skipped', 'true'), ('incognito_mode', 1),
])
def test_malformed_fields_are_ignored_without_aborting_file(field, value):
    rows, report = prepare_streaming_history(json.dumps([{**_row(),field:value},_row()]))
    assert len(rows) == 1 and report['invalid'] == 1


def test_quality_rules_and_whitelist_do_not_turn_skips_into_negative_feedback():
    raw = [dict(_row(uri=f'spotify:track:{i}'), **fields) for i,fields in enumerate([
        {'ms_played': 0}, {'ms_played': 29999}, {'ms_played': 30000,'skipped':True},
        {'incognito_mode':True}, {'ms_played':None}, {'ms_played':True},
        {'ms_played':180000,'ip_addr':'PRIVATE','conn_country':'PRIVATE','platform':'PRIVATE',
         'offline_timestamp':123,'reason_end':'trackdone'},
    ])]
    rows, report = prepare_streaming_history(json.dumps(raw))
    assert len(rows) == 2
    assert rows[0]['skipped'] is True
    assert report['short'] == 2 and report['private'] == 1 and report['unknown_duration'] == 2
    assert 'PRIVATE' not in json.dumps(rows)
    assert set(rows[0]) == {'title','artist','ts','spotify_uri','ms_played','skipped',
                            'private','reason_start','reason_end'}


def test_conflicting_duplicates_are_order_independent_and_private_wins():
    copies = [_row(), {**_row(), 'ms_played':0,'skipped':True}, _row()]
    a, report = prepare_streaming_history(json.dumps(copies))
    b, reverse_report = prepare_streaming_history(json.dumps(copies[::-1]))
    assert a == b and report == reverse_report
    assert len(a) == 1 and a[0]['ms_played'] == 180000
    assert report['duplicates'] == 2 and report['conflicting_duplicates'] == 1
    copies.append({**_row(),'incognito_mode':True,'ms_played':0})
    assert prepare_streaming_history(json.dumps(copies))[0] == []


def test_overlap_and_metadata_changes_use_persistent_spotify_occurrence_id(store):
    iid = store.identities.upsert_identity('main','bridge',None,True)
    store.tracks.upsert_track('one','Song','Artist',None,180)
    store.tracks.upsert_track('two','Other title','Other artist',None,180)
    first = import_spotify_data(store,json.dumps([_row()]))
    again = import_spotify_data(store,json.dumps([_row('Other title','Other artist')]))
    assert first['events_added'] == 1 and again['events_added'] == 0
    assert len(store.history.play_events_since(0)) == 1
    assert store.conn.execute('SELECT COUNT(*) FROM spotify_history_listens WHERE identity_id=?',(iid,)).fetchone()[0] == 1
