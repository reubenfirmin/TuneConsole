"""Saved albums fold their TRACKS into the library on sync, so they count in the taste corpus
(metadata alone doesn't), fetched incrementally, once per album."""
import pytest

from yt_playlist.library import sync
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.util.matching import identity_key
from tests.conftest import FakeClient


class _AlbumClient(FakeClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.album_calls = 0

    def get_library_albums(self, limit=500):
        return [{"browseId": "MPREb_X", "title": "Kind of Blue",
                 "artists": [{"name": "Miles Davis"}], "year": "1959", "thumbnails": [{"url": "t"}]}]

    def get_album(self, browse_id):
        self.album_calls += 1
        return {"tracks": [{"videoId": "v1", "title": "So What", "artists": [{"name": "Miles Davis"}]},
                           {"videoId": "v2", "title": "Freddie Freeloader", "artists": [{"name": "Miles Davis"}]}]}


def test_saved_album_tracks_folded_in_once(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fc = _AlbumClient()

    sync.sync_all(store, {iid: fc}, now=1.0)
    keys = store.rec_query.library_keys()
    assert identity_key("So What", "Miles Davis") in keys        # album track now in the corpus
    assert identity_key("Freddie Freeloader", "Miles Davis") in keys
    assert "MPREb_X" in store.tracks.materialized_album_ids()
    assert fc.album_calls == 1

    sync.sync_all(store, {iid: fc}, now=2.0)                      # already materialized -> no re-fetch
    assert fc.album_calls == 1


def test_incidental_album_track_does_not_prevent_full_materialization(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.tracks.upsert_track("v1", "So What", "Miles Davis", "Kind of Blue", 100,
                       album_browse_id="MPREb_X")
    client = _AlbumClient()
    sync.sync_all(store, {iid: client}, now=1.0)
    assert client.album_calls == 1
    assert identity_key("Freddie Freeloader", "Miles Davis") in store.rec_query.library_keys()
    assert store.collection.cached_album_tracklist_ids() == {"MPREb_X"}


def test_browsing_an_unsaved_album_does_not_prevent_later_library_materialization(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    client = _AlbumClient()
    remember_album(store, "MPREb_X", client.get_album("MPREb_X"),
                   {"title": "Kind of Blue", "artist": "Miles Davis"})
    assert store.collection.cached_album_tracklist_ids() == {"MPREb_X"}
    assert store.collection.complete_album_tracklist_ids() == set()
    sync.sync_all(store, {iid: client}, now=1.0)
    assert client.album_calls == 2
    assert store.collection.complete_album_tracklist_ids() == {"MPREb_X"}


@pytest.mark.parametrize("incomplete", [(), ("album-3",), tuple(f"album-{i}" for i in range(8))])
def test_materialization_enumerates_complete_albums_once_and_preserves_failures(store, monkeypatch, incomplete):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    saved = {f"album-{i}": {"title": f"Album {i}", "artist": "Artist", "thumbnail": None}
             for i in range(8)}
    albums = {bid: {"title": meta["title"], "artists": [{"name": "Artist"}],
                    "trackCount": 2 if bid in incomplete else 1,
                    "tracks": [{"videoId": bid, "title": bid}]}
              for bid, meta in saved.items()}
    bulk = store.collection.complete_album_tracklist_ids
    bulk_calls = 0

    def initial_complete_ids():
        nonlocal bulk_calls
        bulk_calls += 1
        assert bulk_calls == 1, "Post-fetch confirmation must not enumerate unrelated releases"
        return bulk()

    monkeypatch.setattr(store.collection, "complete_album_tracklist_ids", initial_complete_ids)
    result = sync._materialize_album_tracks(store, {iid: FakeClient(albums=albums)}, saved, None)
    assert result["status"] == ("failed" if len(incomplete) == len(saved) else "partial" if incomplete else "complete")
    assert result["refreshed"] == len(saved)
    assert result.get("skipped", 0) == len(incomplete)
    assert bulk_calls == 1
    assert bulk() == saved.keys() - set(incomplete)
