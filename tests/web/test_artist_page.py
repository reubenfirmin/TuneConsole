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
    store.history.add_history_snapshot(iid, 1.0, ["playlist song|artist", "album song|artist"])
    client = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1.0),
                        base_url="http://127.0.0.1")

    response = client.get("/artist", params={"name": "Artist", "browse": "UC_artist"})

    assert response.status_code == 200
    assert "Tracks in your library" in response.text
    assert "synced playlists and saved albums" in response.text
    assert {s["title"] for s in response.context["songs"]} == {"Playlist song", "Album song"}
    assert response.context["total_plays"] == 2
