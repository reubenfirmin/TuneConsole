"""Shared song actions persist to the right account and mirror successful writes locally."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app


SONG = {"video_id": "new-song", "title": "A song's <title>", "artist": "Artist & Friends",
        "album": "New Album", "album_browse": "MPRE_new", "duration": "3:45",
        "thumbnail": "https://example.com/song.jpg"}


def seed_song_actions(store):
    main_id = store.identities.upsert_identity("Main", "main-cred", None, True)
    other_id = store.identities.upsert_identity("Other account", "other-cred", None, False)
    offline_id = store.identities.upsert_identity("Offline", "offline-cred", None, False)
    source = store.playlists.upsert_playlist(main_id, "SOURCE", "Source mix", 1, "h", 1)
    destination = store.playlists.upsert_playlist(other_id, "DEST", "Evening mix", 0, "", 1)
    system = store.playlists.upsert_playlist(main_id, "LM", "Liked Music", 0, "", 1)
    offline = store.playlists.upsert_playlist(offline_id, "OFFLINE", "Offline mix", 0, "", 1)
    track = store.tracks.upsert_track("owned-song", "Owned Song", "Artist & Friends", "Album", 180)
    store.playlists.set_playlist_tracks(source, [track])
    main = FakeClient(albums={"MPRE_new": {
        "title": "New Album", "artists": [{"name": "Artist & Friends"}],
        "tracks": [{"videoId": SONG["video_id"], "title": SONG["title"], "duration": "3:45"}],
    }})
    other = FakeClient()
    clients = {main_id: main, other_id: other}
    app = create_app(store, lambda: clients, now_fn=lambda: 100.0)
    return {"app": app, "main": main, "other": other, "main_id": main_id,
            "source": source, "destination": destination, "system": system, "offline": offline,
            "clients": clients, "store": store}


@pytest.fixture
def songs(store):
    data = seed_song_actions(store)
    with TestClient(data["app"], base_url="http://127.0.0.1") as client:
        yield {**data, "http": client}


def test_destinations_exclude_system_and_disconnected_and_mark_duplicates(songs):
    data = songs["http"].get("/songs/playlists", params={"video_id": "owned-song"}).json()
    assert data == {"multiple_identities": True, "playlists": [
        {"id": songs["destination"], "title": "Evening mix", "identity": "Other account", "already_present": False},
        {"id": songs["source"], "title": "Source mix", "identity": "Main", "already_present": True},
    ]}


def test_create_seeds_an_ordinary_playlist_on_main_account(songs):
    response = songs["http"].post("/songs/create-playlist", json={"name": "  Soul favorites  ", "song": SONG})
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Playlist created."
    assert data["added"] is True
    playlist = songs["store"].playlists.get_playlist(int(data["url"].split("/")[-1]))
    assert playlist.title == "Soul favorites" and playlist.identity_id == songs["main_id"]
    assert playlist.created_at == 100.0
    assert playlist.thumbnail == SONG["thumbnail"]
    assert next(p for p in songs["store"].playlists.get_playlists() if p.id == playlist.id).thumbnail == SONG["thumbnail"]
    tracks = songs["store"].charts.playlist_tracks_detail(playlist.id)
    assert [(t["video_id"], t["duration"], t["album_browse"]) for t in tracks] == [(SONG["video_id"], 225, "MPRE_new")]
    assert playlist.ytm_playlist_id not in songs["store"].playlists.get_playlist_groups()
    assert songs["main"].added == [(playlist.ytm_playlist_id, [SONG["video_id"]])]
    assert not songs["other"].created


def test_add_uses_destination_account_and_preserves_existing_order(songs):
    http, store, destination = songs["http"], songs["store"], songs["destination"]
    store.playlists.set_playlist_tracks(destination, store.playlists.get_playlist_track_ids(songs["source"]))
    response = http.post("/songs/add-to-playlist", json={"playlist_id": destination, "song": SONG})
    assert response.status_code == 200 and response.json()["message"] == "Song added."
    assert songs["other"].added == [("DEST", [SONG["video_id"]])]
    assert not songs["main"].added
    assert [t["video_id"] for t in store.charts.playlist_tracks_detail(destination)] == ["owned-song", SONG["video_id"]]
    assert store.playlists.get_playlist(destination).track_count == 2
    assert store.playlists.get_playlist(destination).thumbnail == SONG["thumbnail"]
    again = http.post("/songs/add-to-playlist", json={"playlist_id": destination, "song": SONG})
    assert "already" in again.json()["message"]
    assert len(songs["other"].added) == 1


@pytest.mark.parametrize("target", ["system", "offline", "missing"])
def test_invalid_destination_does_not_write(songs, target):
    response = songs["http"].post("/songs/add-to-playlist", json={
        "playlist_id": songs.get(target, 999999), "song": SONG})
    assert response.status_code == 422
    assert not songs["main"].added and not songs["other"].added


@pytest.mark.parametrize("body", [
    {"name": " ", "song": SONG}, {"name": "Mix", "song": {**SONG, "video_id": ""}},
    {"name": "Mix", "song": {**SONG, "video_id": "two ids"}},
])
def test_invalid_create_does_not_write(songs, body):
    assert songs["http"].post("/songs/create-playlist", json=body).status_code == 422
    assert not songs["main"].created


def test_disconnected_main_cannot_create(songs):
    songs["clients"].pop(songs["main_id"])
    response = songs["http"].post("/songs/create-playlist", json={"name": "Mix", "song": SONG})
    assert response.status_code == 422 and "not connected" in response.json()["detail"]
    assert not songs["main"].created and not songs["other"].created


def test_remote_create_failure_is_reported_without_local_playlist(songs, monkeypatch):
    def fail(*args):
        raise RuntimeError("Remote unavailable")
    monkeypatch.setattr(songs["main"], "create_playlist", fail)
    before = songs["store"].playlists.get_playlists()
    response = songs["http"].post("/songs/create-playlist", json={"name": "Mix", "song": SONG})
    assert response.status_code == 502
    assert songs["store"].playlists.get_playlists() == before


def test_rejected_song_reports_partial_creation_and_failed_add(songs, monkeypatch):
    def fail(*args):
        raise RuntimeError("Song unavailable")
    monkeypatch.setattr(songs["main"], "add_playlist_items", fail)
    monkeypatch.setattr(songs["other"], "add_playlist_items", fail)
    response = songs["http"].post("/songs/create-playlist", json={"name": "Mix", "song": SONG})
    assert response.status_code == 200
    assert "couldn't add" in response.json()["message"]
    assert response.json()["added"] is False
    assert len(songs["main"].created) == 1
    playlist = songs["store"].playlists.get_playlist(int(response.json()["url"].split("/")[-1]))
    assert playlist.title == "Mix" and playlist.track_count == 0
    assert playlist.thumbnail is None  # A rejected song must not become the cover.
    response = songs["http"].post("/songs/add-to-playlist", json={"playlist_id": songs["destination"], "song": SONG})
    assert response.status_code == 422 and "couldn't add" in response.json()["detail"]
    assert not songs["store"].playlists.get_playlist_track_ids(songs["destination"])


def test_metadata_loads_current_values_originals_and_genre_sources(songs):
    store = songs['store']
    tid = store.playlists.get_playlist_track_ids(songs['source'])[0]
    store.tracks.set_track_title(tid, 'Corrected title')
    store.tracks.set_track_enrichment(tid, 'Electronic', '1995')
    store.enrichment.log_enrichment(tid, 'run', 'discogs', 'genre', 'Art Pop', now=1)
    data = songs['http'].get('/songs/metadata', params={'video_id': 'owned-song'}).json()
    assert data['metadata'] == {
        'track_id': tid, 'title': 'Corrected title', 'artist': 'Artist & Friends',
        'genre': 'Electronic', 'year': '1995', 'orig_title': 'Owned Song', 'orig_artist': 'Artist & Friends',
    }
    assert data['genre_options'] == [{'value': 'Art Pop', 'providers': ['discogs']}]


def test_metadata_saves_all_fields_and_restores_originals(songs, monkeypatch):
    from unittest.mock import Mock

    store = songs['store']
    tid = store.playlists.get_playlist_track_ids(songs['source'])[0]
    store.playlists.set_playlist_tracks(songs['destination'], [tid])
    update = Mock()
    monkeypatch.setattr(songs['app'].state.ctx.rec_worker, 'trigger_content_update', update)
    body = {'song': {'video_id': 'owned-song', 'title': 'Stale display', 'artist': 'Stale artist'},
            'title': ' Fixed title ', 'artist': ' Fixed artist ', 'genre': ' Art Pop ', 'year': ' 1998 '}
    assert songs['http'].post('/songs/metadata', json=body).status_code == 200
    for pid in (songs['source'], songs['destination']):
        track = store.charts.playlist_tracks_detail(pid)[0]
        assert (track['title'], track['artist'], track['genre'], track['year']) == (
            'Fixed title', 'Fixed artist', 'Art Pop', '1998')
    update.assert_called_once_with(video_ids=['owned-song'])
    body.update(title='Owned Song', artist='Artist & Friends', genre='', year='')
    assert songs['http'].post('/songs/metadata', json=body).status_code == 200
    track = store.charts.playlist_tracks_detail(songs['source'])[0]
    assert track['title'] == 'Owned Song' and track['artist'] == 'Artist & Friends'
    assert not track['genre'] and not track['year']
    assert not track['title_edited'] and not track['artist_edited']


def test_metadata_save_preserves_unchanged_provider_selection(songs):
    store = songs['store']
    tid = store.playlists.get_playlist_track_ids(songs['source'])[0]
    store.enrichment.apply_provider_fields(tid, 'discogs', {'genre': 'Electronic', 'year': '1995'})
    body = {'song': {'video_id': 'owned-song', 'title': 'Owned Song'},
            'title': 'Fixed title', 'artist': 'Artist & Friends', 'genre': 'Electronic', 'year': '1995'}
    assert songs['http'].post('/songs/metadata', json=body).status_code == 200
    fields = store.enrichment.metadata_provenance(tid)['fields']
    assert fields['genre']['source'] == fields['mb_year']['source'] == 'discogs'
    assert not fields['genre']['override'] and not fields['mb_year']['override']


def test_metadata_partial_save_preserves_background_enrichment(songs):
    store = songs['store']
    tid = store.playlists.get_playlist_track_ids(songs['source'])[0]
    store.enrichment.apply_provider_fields(tid, 'discogs', {'genre': 'Ambient', 'year': '2022'})
    body = {'song': {'video_id': 'owned-song', 'title': 'Owned Song'}, 'title': 'Fixed title'}
    assert songs['http'].post('/songs/metadata', json=body).status_code == 200
    track = store.tracks.metadata_for_video('owned-song')
    assert track['title'] == 'Fixed title' and track['genre'] == 'Ambient' and track['year'] == '2022'


def test_editing_uncached_song_does_not_acquire_library_membership(songs):
    http, store = songs['http'], songs['store']
    before = store.conn.execute('SELECT * FROM library_memberships').fetchall()
    assert http.get('/songs/metadata', params={'video_id': SONG['video_id']}).json()['metadata'] is None
    body = {'song': SONG, 'title': 'Corrected title', 'artist': SONG['artist'], 'genre': 'Jazz', 'year': '2020'}
    assert http.post('/songs/metadata', json=body).status_code == 200
    track = store.tracks.metadata_for_video(SONG['video_id'])
    assert track['title'] == 'Corrected title' and track['orig_title'] == SONG['title']
    assert track['genre'] == 'Jazz' and track['year'] == '2020'
    assert store.conn.execute('SELECT * FROM library_memberships').fetchall() == before
    assert not songs['main'].added and not songs['other'].added


@pytest.mark.parametrize('change', [{'title': '  '}, {'year': 'last year'}, {'year': '95'}, {'genre': 'x' * 81}])
def test_invalid_metadata_does_not_partially_save(songs, change):
    before = songs['store'].tracks.metadata_for_video('owned-song')
    body = {'song': {'video_id': 'owned-song', 'title': 'Owned Song'},
            'title': 'Updated', 'artist': 'Updated', 'genre': 'Jazz', 'year': '2000', **change}
    assert songs['http'].post('/songs/metadata', json=body).status_code == 422
    assert songs['store'].tracks.metadata_for_video('owned-song') == before


@pytest.mark.parametrize('video_id', ['owned-song', 'uncached-song'])
def test_metadata_write_failure_rolls_back_the_entire_edit(songs, monkeypatch, video_id):
    from yt_playlist.core import catalog

    before = songs['store'].tracks.metadata_for_video(video_id)
    choose_field = catalog.choose_field

    def fail_year(conn, entry_id, field, value, *args, **kwargs):
        if field == 'mb_year':
            raise RuntimeError('write failed')
        return choose_field(conn, entry_id, field, value, *args, **kwargs)

    monkeypatch.setattr(catalog, 'choose_field', fail_year)
    body = {'song': {'video_id': video_id, 'title': 'Original', 'artist': 'Original artist'},
            'title': 'Updated', 'artist': 'Updated artist', 'genre': 'Jazz', 'year': '2000'}
    with pytest.raises(RuntimeError, match='write failed'):
        songs['http'].post('/songs/metadata', json=body)
    assert songs['store'].tracks.metadata_for_video(video_id) == before
