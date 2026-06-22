from fastapi.testclient import TestClient

from yt_playlist import embed
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store, iid):
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1")


def test_refresh_card_advances_rotation_and_rerenders(store):
    iid = store.upsert_identity("main", "cred", None, True)
    tracks = [store.upsert_track(f"v{i}", f"T{i}", f"Art{i}", None, None) for i in range(40)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 40, "h", 0.0), tracks)
    embed.build_and_store(store, dim=4)
    c = _client(store, iid)

    before = store.card_views("wheelhouse")
    r = c.post("/home/refresh-card/wheelhouse")
    assert r.status_code == 200
    assert 'id="gen-wheelhouse"' in r.text                # the one card re-rendered in place
    assert store.card_views("wheelhouse") > before        # rotation advanced to a fresh slice


def test_refresh_card_rejects_unknown_card(store):
    iid = store.upsert_identity("main", "cred", None, True)
    assert _client(store, iid).post("/home/refresh-card/bogus").status_code == 404
