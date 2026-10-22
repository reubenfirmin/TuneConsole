"""Saved album choices are reviewed before writing to the bound destination."""
import json

import pytest
from fastapi.testclient import TestClient

from tests.test_spotify_album_import import AlbumClient, album_zip
from tests.web.test_spotify_data_route import _prepare_library, _wait_for_library_job
from yt_playlist.web.app import create_app


def web_client(store, clients):
    return TestClient(create_app(store, lambda: clients, now_fn=lambda: 1000,
                                 background_work=False), base_url="http://127.0.0.1:8123")


def test_album_preview_binds_release_and_destination_and_report_deletion_preserves_retry(store):
    first = store.identities.upsert_identity("First", "first", None, True)
    second = store.identities.upsert_identity("Destination", "second", None, False)
    clients = {first: AlbumClient(), second: AlbumClient()}
    web = web_client(store, clients)
    token = _prepare_library(web, store, second, payload=album_zip("One"), indices=())
    preview = web.get(f"/import/spotify-library/preview/{token}")
    assert 'Confident album match' in preview.text
    assert 'Destination: <strong>Destination</strong>' in preview.text
    assert 'MPRE-One' in preview.text and 'Import action for One' in preview.text
    assert not clients[second].album_ratings and not store.collection.get_saved_albums()
    clients[second].results.clear()  # The reviewed release cannot drift during confirmation.
    for bad in ({"album_choice_0": "forged"}, {"album_action_0": "forged"}):
        response = web.post("/import/spotify-library/confirm", data={"preview_token": token, **bad})
        assert response.status_code == 400
    assert web.post("/import/spotify-library/confirm", data={
        "preview_token": token, "identity_id": first, "album_choice_0": "MPRE-One"},
        follow_redirects=False).status_code == 303
    assert _wait_for_library_job(store)["report"]["albums_imported"] == 1
    assert clients[first].album_ratings == []
    assert clients[second].album_ratings == [("OLAK-One", "LIKE")]
    assert store.collection.get_saved_albums(identity_id=first) == []
    assert len(store.collection.get_saved_albums(identity_id=second)) == 1

    # The latest report survives app recreation; deleting it retains the imported library
    # and the destination-scoped marker, so a later upload remains idempotent.
    web = web_client(store, clients)
    report = web.get("/import/spotify-library/report")
    assert report.status_code == 200 and 'One' in report.text
    assert 'Delete this report' in report.text
    assert web.post("/import/spotify-library/delete-report", follow_redirects=False).status_code == 303
    assert store.settings.get_setting("spotify_library_import_job") is None
    assert len(store.collection.get_saved_albums(identity_id=second)) == 1
    token = _prepare_library(web, store, second, payload=album_zip("One"), indices=())
    assert 'Previously imported to this account' in web.get(
        f"/import/spotify-library/preview/{token}").text
    web.post("/import/spotify-library/confirm", data={"preview_token": token}, follow_redirects=False)
    assert _wait_for_library_job(store)["report"]["albums_skipped"] == 1
    assert clients[second].album_ratings == [("OLAK-One", "LIKE")]


@pytest.mark.parametrize("choice,action", [("", "save"), ("MPRE-alternate", "save"), ("MPRE-One", "playlist")])
def test_ambiguous_album_needs_review_and_playlist_override_is_explicit(store, choice, action):
    iid = store.identities.upsert_identity("Destination", "bridge", None, True)
    client = AlbumClient()
    client.results["One Artist"].append({**client.results["One Artist"][0], "browseId": "MPRE-alternate"})
    client._albums["MPRE-alternate"] = {**client._albums["MPRE-One"], "audioPlaylistId": "OLAK-alternate"}
    web = web_client(store, {iid: client})
    token = _prepare_library(web, store, iid, payload=album_zip("One"), indices=())
    preview = web.get(f"/import/spotify-library/preview/{token}").text
    assert 'Several releases match' in preview
    assert '<option value="" selected>Leave unselected</option>' in preview
    assert not client.album_ratings and not client.created
    response = web.post("/import/spotify-library/confirm", data={
        "preview_token": token, "album_choice_0": choice, "album_action_0": action}, follow_redirects=False)
    assert response.status_code == 303
    report = _wait_for_library_job(store)["report"]
    if not choice:
        assert report["albums_unmatched"] == 1
        assert client.album_ratings == [] and client.created == []
    elif action == "playlist":
        assert report["albums_as_playlists"] == 1 and report["albums_imported"] == 0
        assert client.added == [(client.created[0][0], ["yt-One"])]
        assert client.album_ratings == [] and store.collection.get_saved_albums() == []
    else:
        assert report["albums_imported"] == 1
        assert client.album_ratings == [("OLAK-alternate", "LIKE")]
        assert store.collection.get_saved_albums()[0]["browse"] == "MPRE-alternate"
    assert json.loads(store.settings.get_setting("spotify_library_import_job"))["identity_id"] == iid
