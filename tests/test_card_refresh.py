from fastapi.testclient import TestClient

from yt_playlist.rec import surfaces
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.test_mode_cards_route import _seed_bundles, prepare_home


def _client(store, iid):
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1")


def test_refresh_card_advances_rotation_and_rerenders(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    _seed_bundles(store)
    prepare_home(store)

    def forbidden(*args, **kwargs):
        raise AssertionError("Card rotation must use prepared offers")

    monkeypatch.setattr(surfaces, "for_you", forbidden)
    monkeypatch.setattr(surfaces, "explore_for_you", forbidden)
    c = _client(store, iid)

    before = store.rec_surface.card_views("wheelhouse")
    r = c.post("/home/refresh-card/wheelhouse")
    assert r.status_code == 200
    assert 'id="gen-wheelhouse"' in r.text                # the one card re-rendered in place
    assert store.rec_surface.card_views("wheelhouse") > before        # rotation advanced to a fresh slice


def test_refresh_card_rejects_unknown_card(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    assert _client(store, iid).post("/home/refresh-card/bogus").status_code == 404
