import re

from fastapi.testclient import TestClient
import pytest

from yt_playlist.rec import surfaces
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only, returning_listener
from tests.onboarding_support import publish_live_mix_inputs
from tests.store_fixture_support import (
    bounded_consumer_schema, file_consumer_store as store, native_sql_calls, without_query_timing,
)


pytestmark = pytest.mark.usefixtures('home_routes_only', 'native_sql_calls', 'without_query_timing')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Card rotation consumes published ranks. Keep real SQL/files,
    # selected-card rendering and view writes without importing other genres.
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        yield schema


def _client(store, iid):
    returning_listener(store, 'mixes', now=1000)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0,
                                 background_work=False),
                      base_url="http://127.0.0.1")


def test_refresh_card_advances_rotation_and_rerenders(store, monkeypatch):
    iid = store.accounts.upsert_account("main", None, True)
    tracks = publish_live_mix_inputs(store, tracks_per_lane=24)
    ranked = {track['video_id'] for track in tracks if track['video_id'].startswith('live-wheelhouse-')}

    def forbidden(*args, **kwargs):
        raise AssertionError("Card rotation must use published ranks")

    monkeypatch.setattr(surfaces, "for_you", forbidden)
    monkeypatch.setattr(surfaces, "explore_for_you", forbidden)
    c = _client(store, iid)

    before = store.rec_ledger.card_views("wheelhouse")
    other_views = {lane: store.rec_ledger.card_views(lane) for lane in ('comfort', 'explore', 'fresh')}
    opening = c.get('/home/cards')
    assert opening.status_code == 200
    shown = set(re.findall(r'data-vid="([^"]+)"', opening.text)) & ranked
    assert len(shown) == 12
    r = c.post("/home/refresh-card/wheelhouse")
    assert r.status_code == 200
    assert re.findall(r'id="gen-([a-z]+)"', r.text) == ['wheelhouse']
    replacement = set(re.findall(r'data-vid="([^"]+)"', r.text))
    assert len(replacement) == 12 and replacement <= ranked
    assert replacement != shown
    assert store.rec_ledger.card_views("wheelhouse") > before
    assert {lane: store.rec_ledger.card_views(lane) for lane in other_views} == other_views


def test_refresh_card_rejects_unknown_card(store):
    iid = store.accounts.upsert_account("main", None, True)
    assert _client(store, iid).post("/home/refresh-card/bogus").status_code == 404
