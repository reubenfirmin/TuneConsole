import io
import json
import zipfile

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.core.store import Store
from yt_playlist.library.spotify_library import (SpotifyLibraryFormatError,
                                                  find_album_candidates,
                                                  import_spotify_library,
                                                  load_spotify_library,
                                                  prepare_spotify_library)


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
            track = {**_track("yt1", "Song", "Artist", album="Record"),
                     "thumbnails": [{"url": "https://example.com/song.jpg"}]}
            self._catalog[track["videoId"]] = track
            return [track]
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
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = LibraryClient()

    report = import_spotify_library(store, _account_zip(), client, identity, 1000.0)
    assert {k: v for k, v in report.items() if k != "details"} == {
        "playlists_found": 1, "playlists_imported": 1, "playlists_skipped": 0, "playlists_failed": 0,
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
        "playlist": "Road songs", "spotify_url": "https://open.spotify.com/track/2", "reason": "unmatched",
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
    assert store.playlists.get_playlists()[0].thumbnail == "https://example.com/song.jpg"
    assert client.added == [(client.created[0][0], ["yt1"])]
    assert client.album_ratings == [("OLAK1", "LIKE")]
    assert store.collection.get_saved_albums()[0]["browse"] == "MPRE1"

    again = import_spotify_library(store, _account_zip(), client, identity, 1001.0)
    assert again["playlists_imported"] == 0 and again["playlists_skipped"] == 1
    assert again["albums_imported"] == 0 and again["albums_skipped"] == 1
    assert again["albums_unmatched"] == 1
    assert again["details"]["playlists"][0]["reason"] == "previously_imported"
    assert again["details"]["albums"][0]["reason"] == "previously_imported"
    assert len(client.created) == 1 and len(client.album_ratings) == 1


def test_same_name_with_different_contents_is_imported():
    store = Store(":memory:"); store.init_schema()
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    track = store.tracks.upsert_track("existing", "Song", "Artist", "Record", 200)
    playlist = store.playlists.upsert_playlist(identity, "PL_EXISTING", "Road songs", 1, "hash", 1.0)
    store.playlists.set_playlist_tracks(playlist, [track])
    client = LibraryClient()

    report = import_spotify_library(store, _account_zip(), client, identity, 1000.0)

    assert report["playlists_imported"] == 1 and report["playlists_skipped"] == 0
    assert len(client.created) == 1


def _playlists_zip(*playlists):
    return _zip({"Playlist1.json": {"playlists": [
        {"name": name, "items": [{"track": {"trackName": title, "artistName": "Artist"}}
                                 for title in titles]} for name, titles in playlists]}})


class SongClient(FakeClient):
    def search(self, query, filter="songs"):
        title = query.removesuffix(" Artist")
        track = _track("yt-" + title, title, "Artist")
        self._catalog[track["videoId"]] = track
        return [track]


def test_identical_contents_in_destination_are_skipped_even_in_a_different_order(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    playlist = store.playlists.upsert_playlist(identity, "PL_EXISTING", "Road songs", 2, "hash", 1)
    ids = [store.tracks.upsert_track(title, title, "Artist", "", 200) for title in ("Two", "One")]
    store.playlists.set_playlist_tracks(playlist, ids)
    client = SongClient()
    report = import_spotify_library(store, _playlists_zip(("Road songs", ["One", "Two"])),
                                    client, identity, 100)
    assert report["details"]["playlists"][0]["reason"] == "already_exists"
    assert client.created == []


def test_import_fingerprints_are_scoped_to_destination(store):
    first = store.identities.upsert_identity("first", "first-bridge", None, True)
    second = store.identities.upsert_identity("second", "second-bridge", None, False)
    raw = _playlists_zip(("Road songs", ["Song"]))
    for identity in (first, second):
        report = import_spotify_library(store, raw, SongClient(), identity, 100)
        assert report["playlists_imported"] == 1
    assert {p.identity_id for p in store.playlists.get_playlists()} == {first, second}


def test_legacy_global_marker_requires_corresponding_destination_contents(store):
    import hashlib
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    fingerprint = "playlist:" + hashlib.sha256(b"road songs\0song|artist").hexdigest()
    store.settings.set_setting("spotify_library_imported_playlists", json.dumps([fingerprint]))
    raw = _playlists_zip(("Road songs", ["Song"]))
    assert import_spotify_library(store, raw, SongClient(), identity, 100)["playlists_imported"] == 1


def test_resume_reconciles_lost_batch_response_after_restart(tmp_path):
    class LostResponse(SongClient):
        fail = True

        def add_playlist_items(self, playlist_id, videos):
            result = super().add_playlist_items(playlist_id, videos)
            if self.fail:
                self.fail = False
                raise RuntimeError("response lost after remote write")
            return result

    path = tmp_path / "state.db"
    store = Store(path); store.init_schema()
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = LostResponse()
    raw = _playlists_zip(("Road songs", ["One", "Two"]))
    assert import_spotify_library(store, raw, client, identity, 100)["playlists_failed"] == 1
    assert store.playlists.get_playlists()[0].track_count == 0
    store.conn.close()
    store = Store(path); store.init_schema()
    try:
        report = import_spotify_library(store, raw, client, identity, 101)
        assert report["playlists_imported"] == 1 and report["tracks_imported"] == 0
        assert report["details"]["playlists"][0]["reason"] == "resumed"
        assert len(client.created) == len(client.added) == 1
        assert store.playlists.get_playlists()[0].track_count == 2
        assert [t["videoId"] for t in client._tracks[client.created[0][0]]] == ["yt-One", "yt-Two"]
    finally:
        store.conn.close()


def test_one_failed_playlist_does_not_stop_the_others(store):
    class FailingClient(SongClient):
        def create_playlist(self, title, description):
            if title == "Broken":
                raise RuntimeError("create rejected")
            return super().create_playlist(title, description)

    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = FailingClient()
    report = import_spotify_library(store, _playlists_zip(("Broken", ["One"]), ("Good", ["Two"])),
                                    client, identity, 100)
    assert report["playlists_failed"] == report["playlists_imported"] == 1
    assert [p["status"] for p in report["details"]["playlists"]] == ["failed", "imported"]
    assert client.created[0][1] == "Good"


def test_ambiguous_tracks_are_previewed_and_never_substituted(store):
    class AmbiguousClient(SongClient):
        def search(self, query, filter="songs"):
            return [_track("studio", "Song", "Artist"), _track("alternate", "Song", "Artist")]

    identity = store.identities.upsert_identity("main", "bridge", None, True)
    raw = _playlists_zip(("Road songs", ["Song"]))
    client = AmbiguousClient()
    prepared = prepare_spotify_library(load_spotify_library(raw), client)
    assert prepared["playlists"][0]["resolution"][0]["status"] == "ambiguous"
    assert client.created == client.added == []
    report = import_spotify_library(store, None, client, identity, 100, prepared=prepared)
    assert report["tracks_unmatched"] == 1 and report["playlists_skipped"] == 1
    assert client.created == []


@pytest.mark.parametrize("result", [
    _track("other-artist", "A very long song title that could hide the wrong artist", "Other"),
    _track("live", "Song (Live)", "Artist"),
])
def test_matching_preserves_artist_and_recording_version(result):
    source_title = "Song" if result["videoId"] == "live" else result["title"]
    client = FakeClient(search_results=[result])
    prepared = prepare_spotify_library({"playlists": [{"name": "Source", "tracks": [
        {"title": source_title, "artist": "Artist", "album": "", "uri": ""}]}], "albums": []}, client)
    assert prepared["playlists"][0]["resolution"][0]["status"] == "unmatched"


def test_unavailable_and_duplicate_search_results_are_classified():
    raw = _playlists_zip(("Source", ["Song"]))
    song = _track("yt", "Song", "Artist")
    for results, expected in (([song, song], "matched"), ([{**song, "isAvailable": False}], "unavailable")):
        prepared = prepare_spotify_library(load_spotify_library(raw), FakeClient(search_results=results))
        assert prepared["playlists"][0]["resolution"][0]["status"] == expected


def test_studio_and_live_rows_have_independent_lookups_and_import_markers(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = SongClient()
    raw = _playlists_zip(("Source", ["Song", "Song (Live)"]))
    prepared = prepare_spotify_library(load_spotify_library(raw), client)
    rows = prepared["playlists"][0]["resolution"]
    assert [row["match"]["videoId"] for row in rows] == ["yt-Song", "yt-Song (Live)"]
    first = import_spotify_library(store, _playlists_zip(("Source", ["Song"])), client, identity, 100)
    assert first["playlists_imported"] == 1
    live = import_spotify_library(store, _playlists_zip(("Source", ["Song (Live)"])), client, identity, 101)
    assert live["playlists_imported"] == 1
    assert len(client.created) == 2


def test_failed_lookup_can_be_recovered_in_the_same_playlist(store, monkeypatch):
    monkeypatch.setattr("yt_playlist.library.spotify_library.with_retry", lambda operation: operation())

    class LookupFailure(SongClient):
        fail = True

        def search(self, query, filter="songs"):
            if self.fail and query == "Two Artist":
                raise RuntimeError("lookup failed")
            return super().search(query, filter)

    identity = store.identities.upsert_identity("main", "bridge", None, True)
    raw = _playlists_zip(("Source", ["One", "Two"]))
    client = LookupFailure()
    first = import_spotify_library(store, raw, client, identity, 100)
    assert first["playlists_failed"] == 1 and first["tracks_imported"] == 1
    client.fail = False
    again = import_spotify_library(store, raw, client, identity, 101)
    assert again["playlists_imported"] == 1 and again["tracks_imported"] == 1
    assert len(client.created) == 1
    assert len(client._tracks[client.created[0][0]]) == 2


def test_album_failure_preserves_playlist_results_and_later_albums(store):
    class AlbumFailure(LibraryClient):
        def __init__(self):
            super().__init__()
            self.failed = False

        def rate_playlist(self, playlist_id, rating):
            if not self.failed:
                self.failed = True
                raise RuntimeError("save rejected")
            super().rate_playlist(playlist_id, rating)

    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = AlbumFailure()
    first = import_spotify_library(store, _account_zip(), client, identity, 100)
    assert first["playlists_imported"] == 1
    assert first["albums_unmatched"] == 2
    assert first["details"]["albums"][0]["reason"] == "lookup_or_save_failed"
    again = import_spotify_library(store, _account_zip(), client, identity, 101)
    assert again["playlists_skipped"] == again["albums_imported"] == 1
    assert len(client.created) == 1


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
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    report = import_spotify_library(store, payload, EditionClient(), identity, 1000.0)

    assert report["albums_imported"] == 1
    assert report["albums_skipped"] == 1
    assert report["albums_unmatched"] == 0
    assert report["details"]["albums"][1]["reason"] == "already_in_library"
