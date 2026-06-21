from fastapi.testclient import TestClient

from yt_playlist import recommend
from yt_playlist.rec_dao import RecDao
from yt_playlist.rec_worker import RecWorker
from yt_playlist.web.app import create_app
from yt_playlist.web.context import Ctx
from tests.conftest import FakeClient


class _ArtistClient(FakeClient):
    """A client that answers the outward-discovery fetch with one new + one owned album."""
    def search(self, query, filter="songs"):
        return [{"browseId": "ART1"}]

    def get_artist(self, browse_id):
        return {"name": "Fav", "description": None, "thumbnails": None, "subscribers": None,
                "albums": {"results": [
                    {"title": "Brand New LP", "year": "2024", "browseId": "ALB_NEW", "thumbnails": None},
                    {"title": "Owned Record", "year": "2010", "browseId": "ALB_OLD", "thumbnails": None},
                ]}}


def _ctx(store, client):
    iid = store.upsert_identity("main", "cred", None, True)
    return iid, Ctx(store=store, client_provider=lambda: {iid: client}, now_fn=lambda: 1.0,
                    templates=None, jobs=None)


def test_new_albums_filters_owned_and_saved(store):
    iid, ctx = _ctx(store, _ArtistClient())
    # "Fav" must rank as a top artist -> needs plays
    t = store.upsert_track("v1", "Song", "Fav", "Owned Record", None)   # owns "Owned Record"
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 1, "h", 0.0), [t])
    store.add_history_snapshot(iid, 1.0, ["song|fav"])

    albums = recommend.new_albums_from_favorites(ctx)
    titles = {a["title"] for a in albums}
    assert "Brand New LP" in titles        # new -> surfaced
    assert "Owned Record" not in titles    # already owned -> filtered


def test_worker_materializes_proposals(store):
    iid, ctx = _ctx(store, _ArtistClient())
    t = store.upsert_track("v1", "Song", "Fav", None, None)
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 1, "h", 0.0), [t])
    store.add_history_snapshot(iid, 1.0, ["song|fav"])

    RecWorker(ctx).rebuild()               # synchronous rebuild + materialize
    dao = RecDao(store)
    assert dao.get_proposals("discover") is not None
    assert any(a["title"] == "Brand New LP" for a in dao.get_proposals("discover"))


def test_home_discover_serves_cached(store):
    iid = store.upsert_identity("main", "cred", None, True)
    RecDao(store).put_proposals("discover", [{"artist": "X", "title": "Cached LP",
                                              "year": "2024", "browse_id": "B", "thumbnail": None}], now=1.0)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    assert "Cached LP" in c.get("/home/discover").text     # served from materialized proposals, no fetch
