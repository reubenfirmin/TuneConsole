import io
import json
import zipfile

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.core.store import Store
from yt_playlist.library.spotify_library import (SpotifyLibraryFormatError,
                                                  find_album_candidates,
                                                  import_spotify_library,
                                                  load_spotify_library)


def _zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, json.dumps(value))
    return out.getvalue()


def _account_zip():
    return _zip({
        "Spotify Account Data/Playlist1.json": {"playlists": [{
            "name": "Road songs", "description": None, "items": [
                {"track": {"trackName": "Song", "artistName": "Artist",
                           "albumName": "Record", "trackUri": "spotify:track:1"}},
                {"track": {"trackName": "Missing", "artistName": "Nobody",
                           "albumName": "Nowhere", "trackUri": "spotify:track:2"}},
                {"episode": {"episodeName": "Podcast"}},
            ]}]},
        "Spotify Account Data/YourLibrary.json": {"albums": [
            {"artist": "Album Artist", "album": "Saved Record", "uri": "spotify:album:1"},
            {"artist": "Nobody", "album": "Missing Record", "uri": "spotify:album:2"},
        ], "tracks": []},
    })


class LibraryClient(FakeClient):
    def __init__(self):
        super().__init__(albums={"MPRE1": {
            "title": "Saved Record", "artists": [{"name": "Album Artist"}],
            "audioPlaylistId": "OLAK1", "type": "Album", "year": "2020",
            "thumbnails": []}})
        self.album_ratings = []

    def search(self, query, filter="songs"):
        if filter == "songs" and query == "Song Artist":
            return [_track("yt1", "Song", "Artist", album="Record")]
        if filter == "albums":
            return [{"browseId": "MPRE1", "title": "Saved Record",
                     "artists": [{"name": "Album Artist"}]}]
        return []

    def rate_playlist(self, playlist_id, rating):
        self.album_ratings.append((playlist_id, rating))


def test_account_data_parser_reads_playlists_and_albums_and_ignores_episodes():
    parsed = load_spotify_library(_account_zip())
    assert parsed["playlists"][0]["name"] == "Road songs"
    assert [t["title"] for t in parsed["playlists"][0]["tracks"]] == ["Song", "Missing"]
    assert parsed["albums"] == [
        {"title": "Saved Record", "artist": "Album Artist", "uri": "spotify:album:1"},
        {"title": "Missing Record", "artist": "Nobody", "uri": "spotify:album:2"},
    ]


def test_technical_log_zip_is_rejected():
    with pytest.raises(SpotifyLibraryFormatError):
        load_spotify_library(_zip({"Spotify Technical Log Information/Download.json": []}))


def test_library_import_creates_matches_and_is_idempotent():
    store = Store(":memory:"); store.init_schema()
    identity = store.upsert_identity("main", "bridge", None, True)
    client = LibraryClient()

    report = import_spotify_library(store, _account_zip(), client, identity, 1000.0)
    assert {k: v for k, v in report.items() if k != "details"} == {
        "playlists_found": 1, "playlists_imported": 1, "playlists_skipped": 0,
        "tracks_imported": 1, "tracks_unmatched": 1,
        "albums_found": 2, "albums_imported": 1, "albums_skipped": 0,
        "albums_unmatched": 1,
    }
    assert report["details"]["playlists"] == [{
        "name": "Road songs", "status": "imported", "reason": "created",
        "tracks_found": 2, "tracks_added": 1, "tracks_unmatched": 1,
        "ytm_playlist_id": client.created[0][0],
    }]
    assert report["details"]["unmatched_tracks"] == [{
        "title": "Missing", "artist": "Nobody", "album": "Nowhere",
        "playlist": "Road songs", "spotify_url": "https://open.spotify.com/track/2",
    }]
    saved, missing = report["details"]["albums"]
    assert saved == {
        "title": "Saved Record", "artist": "Album Artist", "source_uri": "spotify:album:1",
        "spotify_url": "https://open.spotify.com/album/1", "status": "imported",
        "reason": "saved", "browse_id": "MPRE1"}
    assert {k: missing[k] for k in (
        "title", "artist", "source_uri", "spotify_url", "status", "reason")} == {
        "title": "Missing Record", "artist": "Nobody", "source_uri": "spotify:album:2",
        "spotify_url": "https://open.spotify.com/album/2", "status": "unmatched",
        "reason": "no_confident_match"}
    assert missing["candidates"][0]["browse_id"] == "MPRE1"
    assert missing["candidates"][0]["confident"] is False
    assert client.created[0][1] == "Road songs"
    assert client.added == [(client.created[0][0], ["yt1"])]
    assert client.album_ratings == [("OLAK1", "LIKE")]
    assert store.get_saved_albums()[0]["browse"] == "MPRE1"

    again = import_spotify_library(store, _account_zip(), client, identity, 1001.0)
    assert again["playlists_imported"] == 0 and again["playlists_skipped"] == 1
    assert again["albums_imported"] == 0 and again["albums_skipped"] == 1
    assert again["albums_unmatched"] == 1
    assert again["details"]["playlists"][0]["reason"] == "previously_imported"
    assert again["details"]["albums"][0]["reason"] == "previously_imported"
    assert len(client.created) == 1 and len(client.album_ratings) == 1


def test_existing_identical_playlist_is_not_recreated():
    store = Store(":memory:"); store.init_schema()
    identity = store.upsert_identity("main", "bridge", None, True)
    track = store.upsert_track("existing", "Song", "Artist", "Record", 200)
    playlist = store.upsert_playlist(identity, "PL_EXISTING", "Road songs", 1, "hash", 1.0)
    store.set_playlist_tracks(playlist, [track])
    client = LibraryClient()

    report = import_spotify_library(store, _account_zip(), client, identity, 1000.0)

    assert report["playlists_imported"] == 0 and report["playlists_skipped"] == 1
    assert report["details"]["playlists"][0]["reason"] == "name_already_exists"
    assert client.created == []


def test_album_match_treats_expanded_edition_as_the_same_release():
    class EditionClient(LibraryClient):
        def search(self, query, filter="songs"):
            if filter == "albums":
                return [{"browseId": "MPRE_FAT", "title": "The Fat of the Land - Expanded Edition",
                         "artists": [{"name": "The Prodigy"}]}]
            return []

    candidates = find_album_candidates(
        EditionClient(), {"title": "The Fat of the Land", "artist": "The Prodigy"})

    assert candidates[0]["browse_id"] == "MPRE_FAT"
    assert candidates[0]["title_score"] == 1.0
    assert candidates[0]["artist_score"] == 1.0
    assert candidates[0]["confident"] is True


def test_second_spotify_edition_is_skipped_after_equivalent_album_is_saved():
    payload = _zip({"Spotify Account Data/YourLibrary.json": {"albums": [
        {"artist": "The Prodigy", "album": "The Fat of the Land - Expanded Edition",
         "uri": "spotify:album:expanded"},
        {"artist": "The Prodigy", "album": "The Fat of the Land",
         "uri": "spotify:album:original"},
    ]}})

    class EditionClient(LibraryClient):
        def __init__(self):
            super().__init__()
            self._albums["MPRE_FAT"] = {
                "title": "The Fat of the Land - Expanded Edition",
                "artists": [{"name": "The Prodigy"}], "audioPlaylistId": "OLAK_FAT",
                "type": "Album", "thumbnails": []}

        def search(self, query, filter="songs"):
            if filter == "albums":
                return [{"browseId": "MPRE_FAT", "title": "The Fat of the Land - Expanded Edition",
                         "artists": [{"name": "The Prodigy"}]}]
            return []

    store = Store(":memory:"); store.init_schema()
    identity = store.upsert_identity("main", "bridge", None, True)
    report = import_spotify_library(store, payload, EditionClient(), identity, 1000.0)

    assert report["albums_imported"] == 1
    assert report["albums_skipped"] == 1
    assert report["albums_unmatched"] == 0
    assert report["details"]["albums"][1]["reason"] == "already_in_library"
