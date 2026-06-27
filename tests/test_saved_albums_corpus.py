"""Saved albums fold their TRACKS into the library on sync, so they count in the taste corpus
(metadata alone doesn't), fetched incrementally, once per album."""
from yt_playlist.library import sync
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
    iid = store.upsert_identity("main", "cred", None, True)
    fc = _AlbumClient()

    sync.sync_all(store, {iid: fc}, now=1.0)
    keys = store.library_keys()
    assert identity_key("So What", "Miles Davis") in keys        # album track now in the corpus
    assert identity_key("Freddie Freeloader", "Miles Davis") in keys
    assert "MPREb_X" in store.materialized_album_ids()
    assert fc.album_calls == 1

    sync.sync_all(store, {iid: fc}, now=2.0)                      # already materialized -> no re-fetch
    assert fc.album_calls == 1
