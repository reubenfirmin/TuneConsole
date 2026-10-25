from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.charts import _more_albums


def test_more_albums_excludes_only_the_same_release():
    collection = [{"album": "Strawberry Hotel", "browse": "MPREb_strawberry"}]
    discography = [
        {"title": "Strawberry Hotel", "browse_id": "MPREb_strawberry"},
        {"title": "  STRAWBERRY HOTEL ", "browse_id": "different_duplicate"},
        {"title": "Dubnobasswithmyheadman", "browse_id": "MPREb_dubno"},
    ]

    assert _more_albums(discography, collection) == discography[1:]


def test_artist_page_includes_album_tracks_when_following_artist_index_link(store, monkeypatch):
    monkeypatch.setattr("yt_playlist.web.routes.charts.fetch_artist_info", lambda *args: None)
    iid = store.identities.upsert_identity("me", "c", None, True)
    playlist = store.playlists.upsert_playlist(iid, "PL1", "Mix", 0, "h", 0.0)
    track = store.tracks.upsert_track("v1", "Playlist song", "Artist", "First", 180,
                               artist_browse_id="UC_artist")
    store.playlists.set_playlist_tracks(playlist, [track])
    store.collection.add_saved_album({"browse": "B1", "title": "Second", "artist": "Artist"})
    store.tracks.upsert_track("v2", "Album song", "Artist", "Second", 180, album_browse_id="B1",
                             artist_browse_id='UC_artist')
    store.history.record_history_plays(iid, 1.0, ["playlist song|artist", "album song|artist"], source_timezone="UTC")
    client = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1.0),
                        base_url="http://127.0.0.1")

    response = client.get("/artist", params={"name": "Artist", "browse": "UC_artist"})

    assert response.status_code == 200
    assert "Tracks in your library" in response.text
    assert "synced playlists and saved albums" in response.text
    assert {s["title"] for s in response.context["songs"]} == {"Playlist song", "Album song"}
    assert response.context["total_plays"] == 2


def test_artist_catalog_channel_resolves_ambiguous_name_without_merging_credits(store, monkeypatch):
    iid = store.identities.upsert_identity("me", "c", None, True)
    playlist = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 0)
    owned = store.tracks.upsert_track("owned", "Library song", "Same", "First", 180,
                                    artist_browse_id="UC_verified", album_browse_id="B1")
    store.playlists.set_playlist_tracks(playlist, [owned])
    # A distinct identity with the same credit must not erase or acquire this membership.
    other = store.tracks.upsert_track("other", "Other artist song", "Same", "Other", 180,
                                    artist_browse_id="UC_other", album_browse_id="B2")
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PL2", "Other mix", 1, "h", 0), [other])
    assert store.artists.resolve("Same") is None
    monkeypatch.setattr("yt_playlist.web.routes.charts.fetch_artist_info", lambda *args:
                        {"browse_id": "UC_verified", "albums": []})
    client = TestClient(create_app(store, lambda: {}, background_work=False), base_url="http://127.0.0.1")
    response = client.get("/artist", params={"name": "Same"})
    assert response.status_code == 200
    assert [s['title'] for s in response.context['songs']] == ['Library song']
    assert response.context['songs'][0]['playlists'] == [{'title': 'Mix', 'ytm': 'PL1'}]
    assert response.context['collection'][0]['n_pls'] == 1
    assert store.artists.resolve("Same") is None


def test_new_catalog_channel_does_not_hide_unambiguous_library_artist(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    track = store.tracks.upsert_track("v", "Song", "Artist", "Album", 180, album_browse_id="B")
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "P", "Mix", 1, "h", 0), [track])
    assert store.charts.artist_songs("Artist", "new-channel")[0]['playlists'] == [{'title': 'Mix', 'ytm': 'P'}]
    assert store.collection.artist_albums("Artist", "new-channel")[0]['n_pls'] == 1


def test_artist_albums_blend_saved_and_catalog_releases_in_newest_first_order(store, monkeypatch):
    store.collection.add_saved_album({'browse': 'saved', 'title': 'Saved album', 'artist': 'Artist', 'year': '2000'})
    monkeypatch.setattr("yt_playlist.web.routes.charts.fetch_artist_info", lambda *args: {'albums': [
        {'browse_id': 'new', 'title': 'New release', 'year': '2024'},
        {'browse_id': 'old', 'title': 'Old release', 'year': '1990'},
        {'browse_id': 'unknown', 'title': 'Undated', 'year': None}]})
    client = TestClient(create_app(store, lambda: {}, background_work=False), base_url="http://127.0.0.1")
    response = client.get('/artist', params={'name': 'Artist'})
    assert [a['album'] for a in response.context['albums']] == ['New release', 'Saved album', 'Old release', 'Undated']
    assert response.text.count('colgroup class="artist-album-columns"') == 1
    assert 'More albums on YouTube' not in response.text
    assert 'ui-arrow--external' not in response.text
