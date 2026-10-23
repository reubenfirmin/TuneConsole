"""Album previews must bind a destination and release before library writes."""
import json

import pytest

from tests.conftest import FakeClient, _track
from tests.test_spotify_library_import import _zip
from yt_playlist.library.spotify_library import (
    import_spotify_library, load_spotify_library, prepare_spotify_library)


class AlbumClient(FakeClient):
    def __init__(self):
        albums = {f"MPRE-{name}": {
            "title": name, "artists": [{"name": "Artist"}], "year": "2001",
            "audioPlaylistId": f"OLAK-{name}", "trackCount": 1,
            "tracks": [_track(f"yt-{name}", f"{name} song", "Artist", album=name)],
        } for name in ("One", "Two", "Three")}
        super().__init__(albums=albums, catalog={
            track["videoId"]: track for album in albums.values() for track in album["tracks"]})
        self.results = {f"{album['title']} Artist": [{
            "browseId": browse, "title": album["title"], "artists": album["artists"], "year": album["year"],
        }] for browse, album in albums.items()}
        self.searches, self.album_ratings = [], []
        self.failed_searches, self.failed_saves = set(), set()

    def search(self, query, filter="songs"):
        self.searches.append(query)
        if query in self.failed_searches:
            raise RuntimeError("lookup failed")
        return self.results.get(query, []) if filter == "albums" else []

    def rate_playlist(self, playlist_id, rating):
        if playlist_id in self.failed_saves:
            return {"status": "STATUS_FAILED"}
        self.album_ratings.append((playlist_id, rating))
        return {"status": "STATUS_SUCCEEDED"}


def album_zip(*titles):
    return _zip({"YourLibrary.json": {"albums": [
        {"album": title, "artist": "Artist", "uri": f"spotify:album:{title}"} for title in titles]}})


def test_album_preview_detects_ambiguity_before_any_save_and_keeps_confirmed_release(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = AlbumClient()
    alternate = {**client.results["One Artist"][0], "browseId": "MPRE-alternate"}
    client.results["One Artist"].append(alternate)
    prepared = prepare_spotify_library(load_spotify_library(album_zip("One", "Two")), client,
                                       store=store, identity_id=iid)
    assert [a["resolution"]["status"] for a in prepared["albums"]] == ["ambiguous", "matched"]
    assert prepared["albums"][0]["resolution"]["match"] is None
    assert client.album_ratings == [] and store.collection.get_saved_albums() == []
    # Confirmation uses the reviewed release even if search results subsequently change.
    client.results = {}
    report = import_spotify_library(store, None, client, iid, 100, prepared=prepared)
    assert report["albums_imported"] == report["albums_unmatched"] == 1
    assert report["details"]["albums"][0]["reason"] == "ambiguous"
    assert client.album_ratings == [("OLAK-Two", "LIKE")]
    assert client.searches == ["One Artist", "Two Artist"]


def test_repeated_search_rows_do_not_make_a_release_ambiguous():
    client = AlbumClient()
    client.results["One Artist"] *= 3
    prepared = prepare_spotify_library(load_spotify_library(album_zip("One")), client)
    resolution = prepared["albums"][0]["resolution"]
    assert resolution["status"] == "matched"
    assert len(resolution["candidates"]) == 1


def test_ambiguity_includes_confident_releases_beyond_display_limit():
    client = AlbumClient()
    client.results["One Artist"] = [
        {**client.results["One Artist"][0], "browseId": f"MPRE-{index}"} for index in range(8)]
    resolution = prepare_spotify_library(load_spotify_library(album_zip("One")), client)["albums"][0]["resolution"]
    assert resolution["status"] == "ambiguous" and resolution["match"] is None
    assert len(resolution["candidates"]) == 8


def test_export_release_year_prevents_confident_wrong_year():
    client = AlbumClient()
    parsed = load_spotify_library(_zip({"YourLibrary.json": {"albums": [{
        "album": "One", "artist": "Artist", "releaseDate": "1990-01-01"}]}}))
    assert parsed["albums"][0]["year"] == "1990"
    assert prepare_spotify_library(parsed, client)["albums"][0]["resolution"]["status"] == "unmatched"


def test_preview_and_import_deduplicate_only_within_the_destination(store):
    first = store.identities.upsert_identity("First", "bridge", None, True)
    second = store.identities.upsert_identity("Second", "bridge", None, False)
    client = AlbumClient()
    raw = album_zip("One")
    assert import_spotify_library(store, raw, client, first, 100)["albums_imported"] == 1
    client.searches.clear()
    own = prepare_spotify_library(load_spotify_library(raw), client, store=store, identity_id=first)
    assert own["albums"][0]["resolution"]["status"] == "skipped"
    assert client.searches == []
    other = prepare_spotify_library(load_spotify_library(raw), client, store=store, identity_id=second)
    assert other["albums"][0]["resolution"]["status"] == "matched"
    assert import_spotify_library(store, None, client, second, 101, prepared=other)["albums_imported"] == 1
    assert client.album_ratings == [("OLAK-One", "LIKE")] * 2


@pytest.mark.parametrize("failure", ["lookup", "save"])
def test_failure_between_successes_is_isolated_and_retry_keeps_successes(store, monkeypatch, failure):
    monkeypatch.setattr("yt_playlist.library.spotify_library.with_retry", lambda operation: operation())
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = AlbumClient()
    failed = client.failed_searches if failure == "lookup" else client.failed_saves
    failed.add("Two Artist" if failure == "lookup" else "OLAK-Two")
    report = import_spotify_library(store, album_zip("One", "Two", "Three"), client, iid, 100)
    assert report["albums_imported"] == 2 and report["albums_unmatched"] == 1
    assert [row["status"] for row in report["details"]["albums"]] == ["imported", "unmatched", "imported"]
    assert {row["browse"] for row in store.collection.get_saved_albums(identity_id=iid)} == {"MPRE-One", "MPRE-Three"}
    failed.clear()
    again = import_spotify_library(store, album_zip("One", "Two", "Three"), client, iid, 101)
    assert again["albums_imported"] == 1 and again["albums_skipped"] == 2
    assert client.album_ratings == [("OLAK-One", "LIKE"), ("OLAK-Three", "LIKE"), ("OLAK-Two", "LIKE")]


def test_catalog_fallback_uses_youtube_candidate_and_does_not_fabricate_listens(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = AlbumClient()
    del client._albums["MPRE-One"]["title"]
    del client._albums["MPRE-One"]["artists"]
    client.results["ONE Artist"] = client.results["One Artist"]
    report = import_spotify_library(store, album_zip("ONE"), client, iid, 100)
    assert report["albums_imported"] == 1
    assert store.collection.get_saved_albums()[0]["title"] == "One"
    assert report["details"]["albums"][0]["title"] == "ONE"
    assert store.conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM listening_days").fetchone()[0] == 0


def test_explicit_album_playlist_uses_destination_journal_and_survives_retry(store):
    iid = store.identities.upsert_identity("Destination", "bridge", None, True)
    client = AlbumClient()
    prepared = prepare_spotify_library(load_spotify_library(album_zip("One")), client)
    prepared["albums"][0]["action"] = "playlist"
    first = import_spotify_library(store, None, client, iid, 100, prepared=prepared)
    assert first["albums_as_playlists"] == 1 and first["albums_imported"] == 0
    assert client.album_ratings == [] and store.collection.get_saved_albums() == []
    assert client.added == [(client.created[0][0], ["yt-One"])]
    assert store.playlists.get_playlists()[0].identity_id == iid
    again = import_spotify_library(store, None, client, iid, 101, prepared=prepared)
    assert again["albums_as_playlists"] == 1 and again["tracks_imported"] == 0
    assert len(client.created) == len(client.added) == 1
    state = json.loads(store.settings.get_setting(f"spotify_library_imported_playlists:{iid}"))
    assert next(iter(state.values()))["complete"]
