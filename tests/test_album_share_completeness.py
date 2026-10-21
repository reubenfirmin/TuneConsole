"""Album downloads require a full, ordered, shareable snapshot, including offline exports."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.core.store import Store
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.web.app import create_app


def _album():
    return {"title": "Album", "artists": [{"name": "Artist"}], "trackCount": 3,
            "tracks": [{"videoId": video, "title": title} for video, title in
                       [("v2", "Second"), ("v1", "First"), ("v2", "Second")]]}


def _client(store, album=None):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.collection.replace_saved_albums([{"browse": "MPRE", "title": "Album", "artist": "Artist"}])
    client = FakeClient(albums={"MPRE": album}) if album is not None else None
    return TestClient(create_app(store, lambda: {iid: client} if client else {}, now_fn=lambda: 1),
                      base_url="http://127.0.0.1")


def _materialize(store):
    for track in _album()["tracks"]:
        store.tracks.upsert_track(track["videoId"], track["title"], "Artist", "Album", 200,
                           album_browse_id="MPRE")
    remember_album(store, "MPRE", _album())


def test_saved_metadata_and_incidental_tracks_require_a_full_live_read(store):
    store.tracks.upsert_track("v1", "First", "Artist", "Album", 200, album_browse_id="MPRE")
    response = _client(store, _album()).get("/album/MPRE/share.txt")
    assert response.status_code == 200
    assert [line.rsplit("=", 1)[1] for line in response.text.splitlines()] == ["v2", "v1", "v2"]


def test_complete_cached_export_preserves_order_duplicates_and_supports_offline(store):
    # Incidental insertion order must not dictate the exported album order.
    store.tracks.upsert_track("v1", "First", "Artist", "Album", 200, album_browse_id="MPRE")
    _materialize(store)
    response = _client(store).get("/album/MPRE/share.txt")
    assert response.status_code == 200
    assert [line.rsplit("=", 1)[1] for line in response.text.splitlines()] == ["v2", "v1", "v2"]
    assert "attachment" in response.headers["content-disposition"]


@pytest.mark.parametrize("problem", ["offline", "continuation", "continuations", "count", "invalid-count", "unsupported", "empty"])
def test_failed_or_incomplete_live_read_never_downloads_a_partial_file(store, problem):
    store.tracks.upsert_track("v1", "First", "Artist", "Album", 200, album_browse_id="MPRE")
    album = _album()
    if problem in ("continuation", "continuations"):
        album[problem] = ["next"]
    elif problem == "count":
        album["trackCount"] = 10
    elif problem == "invalid-count":
        album["trackCount"] = "unknown"
    elif problem == "unsupported":
        album["tracks"][1].pop("videoId")
    elif problem == "empty":
        album["tracks"] = []
    response = _client(store, None if problem == "offline" else album).get("/album/MPRE/share.txt")
    assert response.status_code == 503
    assert "content-disposition" not in response.headers
    assert "try" in response.json()["detail"].lower()
    assert store.collection.cached_album_share("MPRE") is None


def test_materialization_boundary_remains_required_even_with_an_ordered_snapshot(store):
    _materialize(store)
    assert store.collection.cached_album_share("MPRE") is not None
    store.conn.execute("DELETE FROM album_track_links WHERE video_id='v1'")
    store.conn.commit()
    assert _client(store).get("/album/MPRE/share.txt").status_code == 503


def test_a_new_full_but_unshareable_tracklist_invalidates_the_previous_export(store):
    _materialize(store)
    assert store.collection.cached_album_share("MPRE") is not None
    album = _album()
    album["tracks"][1].pop("videoId")
    remember_album(store, "MPRE", album)
    assert _client(store).get("/album/MPRE/share.txt").status_code == 503


def test_upgrade_preserves_denominators_without_inventing_original_order():
    store = Store(":memory:")
    migrate(store.conn, registry=MIGRATIONS[:14])
    store.conn.execute("INSERT INTO album_tracklists VALUES ('MPRE','Album','Artist',NULL,'[\"first|artist\",\"second|artist\"]')")
    store.conn.commit()
    store.init_schema()
    assert store.conn.execute("SELECT track_keys FROM album_tracklists").fetchone()[0] == '["first|artist","second|artist"]'
    assert store.conn.execute("SELECT COUNT(*) FROM album_share_snapshots").fetchone()[0] == 0
    store.init_schema()
    assert store.conn.execute("SELECT COUNT(*) FROM album_tracklists").fetchone()[0] == 1
