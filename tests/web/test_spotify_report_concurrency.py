"""An in-flight album operation owns its report until its provider work settles."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from tests.test_spotify_album_import import AlbumClient, album_zip
from tests.web.test_spotify_data_route import _prepare_library, _select_library, _wait_for_library_job
from yt_playlist.library.spotify_library import import_spotify_library, SPOTIFY_REPORT_SCHEMA
from yt_playlist.web.app import create_app
from yt_playlist.web import spotify_import
from yt_playlist.web.spotify_import import JOB_KEY, save_job


@pytest.mark.parametrize("operation", ["find-album-candidates", "resolve-album"])
@pytest.mark.parametrize("overlap", ["delete", "replace"])
def test_album_operation_rejects_overlapping_report_deletion_or_replacement(store, operation, overlap):
    first = store.identities.upsert_identity("First", "first", None, True)
    second = store.identities.upsert_identity("Second", "second", None, False)
    entered, release = threading.Event(), threading.Event()

    class BlockingClient(AlbumClient):
        blocking = False

        def search(self, query, filter="songs"):
            if self.blocking and operation == "find-album-candidates":
                entered.set()
                assert release.wait(5), "The test did not release the provider lookup"
            return super().search(query, filter)

        def rate_playlist(self, playlist_id, rating):
            if self.blocking and operation == "resolve-album":
                entered.set()
                assert release.wait(5), "The test did not release the provider save"
            return super().rate_playlist(playlist_id, rating)

    old_client, new_client = BlockingClient(), AlbumClient()
    old_client.results["One Artist"].append({
        **old_client.results["One Artist"][0], "browseId": "MPRE-alternate"})
    report = import_spotify_library(store, album_zip("One"), old_client, first, 100)
    assert report["albums_unmatched"] == 1
    save_job(store, {"status": "done", "identity_id": first, "identity_label": "First",
                     "report_schema": SPOTIFY_REPORT_SCHEMA, "report": report})
    app = create_app(store, lambda: {first: old_client, second: new_client},
                     now_fn=lambda: 1000, background_work=False)
    web = TestClient(app, base_url="http://127.0.0.1:8123")
    other = TestClient(app, base_url="http://127.0.0.1:8123")
    token, _ = _select_library(other, album_zip("Two"))
    replacement = {"selection_token": token, "identity_id": second, "include_albums": "on"}
    baseline = store.settings.get_setting(JOB_KEY)
    old_client.blocking = True

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(web.post, "/import/spotify-library/" + operation,
                              data={"album_index": "0", "browse_id": "MPRE-One", "identity_id": second},
                              follow_redirects=False)
        try:
            assert entered.wait(3), "The album operation did not reach the provider"
            if overlap == "replace":
                response = other.post("/import/spotify-library/prepare", data=replacement)
            else:
                response = other.post("/import/spotify-library/delete-report", follow_redirects=False)
            assert response.status_code == 409
            assert store.settings.get_setting(JOB_KEY) == baseline
            # Another manual operation must share the same owner, too.
            assert other.post("/import/spotify-library/resolve-album", data={
                "album_index": "0", "browse_id": "MPRE-One"}, follow_redirects=False).status_code == 409
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 303

    completed = json.loads(store.settings.get_setting(JOB_KEY))
    assert completed["identity_id"] == first
    if operation == "resolve-album":
        assert completed["report"]["albums_imported"] == 1
        assert old_client.album_ratings == [("OLAK-One", "LIKE")]
        assert len(store.collection.get_saved_albums(identity_id=first)) == 1
        retry_markers = store.settings.get_setting(f"spotify_library_imported_albums:{first}")
        assert retry_markers
    else:
        assert completed["report"]["details"]["albums"][0]["candidates"]
        assert old_client.album_ratings == []
    assert new_client.album_ratings == []
    assert store.collection.get_saved_albums(identity_id=second) == []

    # Once accepted, deletion stays deleted and replacement keeps its own preview.
    if overlap == "replace":
        assert other.post("/import/spotify-library/prepare", data=replacement).status_code == 200
        latest = _wait_for_library_job(store)
        assert latest["status"] == "preview" and latest["identity_id"] == second
        assert latest["preview_token"]
    else:
        assert other.post("/import/spotify-library/delete-report", follow_redirects=False).status_code == 303
        assert store.settings.get_setting(JOB_KEY) is None
    if operation == "resolve-album":
        assert store.settings.get_setting(f"spotify_library_imported_albums:{first}") == retry_markers
        assert len(store.collection.get_saved_albums(identity_id=first)) == 1


@pytest.mark.parametrize("operation", ["find-album-candidates", "resolve-album"])
def test_failed_album_operation_releases_report_owner(store, operation):
    iid = store.identities.upsert_identity("First", "first", None, True)
    client = AlbumClient()
    client.results["One Artist"].append({**client.results["One Artist"][0], "browseId": "MPRE-alternate"})
    report = import_spotify_library(store, album_zip("One"), client, iid, 100)
    save_job(store, {"status": "done", "identity_id": iid,
                     "report_schema": SPOTIFY_REPORT_SCHEMA, "report": report})
    client.failed_searches.add("One Artist")
    client.failed_saves.add("OLAK-One")
    web = TestClient(create_app(store, lambda: {iid: client}, now_fn=lambda: 1000,
                                background_work=False), base_url="http://127.0.0.1:8123")
    response = web.post("/import/spotify-library/" + operation,
                        data={"album_index": "0", "browse_id": "MPRE-One"}, follow_redirects=False)
    assert response.status_code == 303
    assert "spotify_error=" in response.headers["location"]
    assert json.loads(store.settings.get_setting(JOB_KEY))["report"]["albums_unmatched"] == 1
    assert web.post("/import/spotify-library/delete-report", follow_redirects=False).status_code == 303
    assert store.settings.get_setting(JOB_KEY) is None


@pytest.mark.parametrize("handoff", ["completed", "replacement", "deleted"])
def test_status_poll_preserves_report_completed_or_replaced_during_its_read(store, monkeypatch, handoff):
    first = store.identities.upsert_identity("First", "first", None, True)
    second = store.identities.upsert_identity("Second", "second", None, False)
    provider_entered, finish_provider = threading.Event(), threading.Event()
    poll_read, finish_poll = threading.Event(), threading.Event()

    class BlockingClient(AlbumClient):
        def rate_playlist(self, playlist_id, rating):
            provider_entered.set()
            assert finish_provider.wait(5), "The test did not release the album save"
            return super().rate_playlist(playlist_id, rating)

    remote, next_remote = BlockingClient(), AlbumClient()
    app = create_app(store, lambda: {first: remote, second: next_remote},
                     now_fn=lambda: 1000, background_work=False)
    web = TestClient(app, base_url="http://127.0.0.1:8123")
    other = TestClient(app, base_url="http://127.0.0.1:8123")
    preview = _prepare_library(web, store, first, payload=album_zip("One"), indices=())
    selection, _ = _select_library(other, album_zip("Two"))
    original_load = spotify_import.load_job

    def delayed_read(current_store):
        captured = original_load(current_store)
        if captured and captured.get("status") in spotify_import.ACTIVE_STATUSES and not poll_read.is_set():
            poll_read.set()
            assert finish_poll.wait(5), "The test did not release the status poll"
        return captured

    assert web.post("/import/spotify-library/confirm", data={"preview_token": preview},
                    follow_redirects=False).status_code == 303
    try:
        assert provider_entered.wait(3), "The import did not reach the album save"
        monkeypatch.setattr(spotify_import, "load_job", delayed_read)
        with ThreadPoolExecutor(max_workers=1) as pool:
            polling = pool.submit(other.get, "/import/spotify-library/status")
            try:
                assert poll_read.wait(3), "The status poll did not capture the running job"
                # Polling while the worker owns the import stays read-only and returns promptly.
                active = web.get("/import/spotify-library/status")
                assert active.status_code == 200 and "HX-Redirect" not in active.headers
                assert original_load(store)["status"] == "running"
                finish_provider.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    completed = original_load(store)
                    if completed["status"] == "done" and not app.state.ctx.spotify_import_lock.locked():
                        break
                    time.sleep(.01)
                assert completed["status"] == "done"
                assert not app.state.ctx.spotify_import_lock.locked()
                assert completed["report"]["albums_imported"] == 1
                retry_markers = store.settings.get_setting(f"spotify_library_imported_albums:{first}")
                assert retry_markers
                if handoff == "replacement":
                    assert web.post("/import/spotify-library/prepare", data={
                        "selection_token": selection, "identity_id": second,
                        "include_albums": "on"}).status_code == 200
                    latest = _wait_for_library_job(store)
                    assert latest["status"] == "preview" and latest["identity_id"] == second
                elif handoff == "deleted":
                    assert web.post("/import/spotify-library/delete-report",
                                    follow_redirects=False).status_code == 303
                expected = original_load(store)
            finally:
                finish_poll.set()
            response = polling.result(timeout=3)
            assert response.status_code == (204 if handoff == "deleted" else 200)
            if handoff == "completed":
                assert response.headers["HX-Redirect"] == "/#notices"
            elif handoff == "replacement":
                assert response.headers["HX-Redirect"].endswith(expected["preview_token"])
    finally:
        finish_provider.set()
        finish_poll.set()

    assert original_load(store) == expected
    assert remote.album_ratings == [("OLAK-One", "LIKE")]
    assert next_remote.album_ratings == []
    assert len(store.collection.get_saved_albums(identity_id=first)) == 1
    assert store.settings.get_setting(f"spotify_library_imported_albums:{first}") == retry_markers


@pytest.mark.parametrize("status", ["queued", "running"])
def test_status_poll_recovers_a_genuinely_interrupted_import(store, status):
    identity = store.identities.upsert_identity("Destination", "credential", None, True)
    previous = {"status": status, "identity_id": identity, "started_at": 900}
    save_job(store, previous)
    app = create_app(store, lambda: {identity: AlbumClient()}, now_fn=lambda: 1000,
                     background_work=False)
    web = TestClient(app, base_url="http://127.0.0.1:8123")
    response = web.get("/import/spotify-library/status")
    assert response.status_code == 200
    assert "TuneConsole stopped before the import finished" in response.text
    recovered = spotify_import.load_job(store)
    assert recovered["status"] == "error" and recovered["finished_at"] == 1000
    assert recovered["identity_id"] == identity and recovered["started_at"] == 900
    assert not app.state.ctx.spotify_import_lock.locked()
    assert web.get("/import/spotify-library/status").status_code == 200
    assert spotify_import.load_job(store) == recovered
