from yt_playlist import recommend
from yt_playlist.rec_dao import RecDao
from yt_playlist.rec_worker import RecWorker
from yt_playlist.web.context import Ctx
from tests.conftest import FakeClient


class _RadioClient(FakeClient):
    def get_watch_playlist(self, videoId):
        return {"tracks": [
            {"videoId": "OWNED", "title": "Hit", "artists": [{"name": "Fav"}], "thumbnails": None},
            {"videoId": "NEWV", "title": "Brand New Song", "artists": [{"name": "Newcomer"}], "thumbnails": None},
        ]}


def _ctx(store, client):
    iid = store.upsert_identity("main", "cred", None, True)
    return iid, Ctx(store=store, client_provider=lambda: {iid: client}, now_fn=lambda: 1.0,
                    templates=None, jobs=None)


def test_fresh_songs_excludes_owned(store):
    iid, ctx = _ctx(store, _RadioClient())
    t = store.upsert_track("v1", "Hit", "Fav", None, None)        # owns "Hit" (key hit|fav)
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 1, "h", 0.0), [t])
    store.add_history_snapshot(iid, 1.0, ["hit|fav"])             # top track seeds the radio

    songs = recommend.fresh_songs(ctx)
    titles = {s["title"] for s in songs}
    assert "Brand New Song" in titles        # unowned -> surfaced
    assert "Hit" not in titles               # already owned -> filtered


def test_worker_materializes_fresh_songs(store):
    iid, ctx = _ctx(store, _RadioClient())
    t = store.upsert_track("v1", "Hit", "Fav", None, None)
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 1, "h", 0.0), [t])
    store.add_history_snapshot(iid, 1.0, ["hit|fav"])
    RecWorker(ctx).rebuild()
    assert any(s["title"] == "Brand New Song" for s in RecDao(store).get_proposals("fresh_songs"))
