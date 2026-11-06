from fastapi.testclient import TestClient
import pytest

from yt_playlist.rec import surfaces
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only, publish_menu, returning_listener


pytestmark = pytest.mark.usefixtures('home_routes_only')


def _client(store, iid):
    returning_listener(store, 'mixes', now=1000)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0,
                                 background_work=False),
                      base_url="http://127.0.0.1")


def test_refresh_card_advances_rotation_and_rerenders(store, monkeypatch):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    publish_menu(store)

    def forbidden(*args, **kwargs):
        raise AssertionError("Card rotation must use prepared offers")

    monkeypatch.setattr(surfaces, "for_you", forbidden)
    monkeypatch.setattr(surfaces, "explore_for_you", forbidden)
    c = _client(store, iid)

    before = store.rec_ledger.card_views("wheelhouse")
    other_views = {lane: store.rec_ledger.card_views(lane) for lane in ('comfort', 'explore')}
    r = c.post("/home/refresh-card/wheelhouse")
    assert r.status_code == 200
    assert 'id="gen-wheelhouse"' in r.text                # the one card re-rendered in place
    assert store.rec_ledger.card_views("wheelhouse") > before        # rotation advanced to a fresh slice
    assert {lane: store.rec_ledger.card_views(lane) for lane in other_views} == other_views


def test_refresh_card_rejects_unknown_card(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    assert _client(store, iid).post("/home/refresh-card/bogus").status_code == 404
