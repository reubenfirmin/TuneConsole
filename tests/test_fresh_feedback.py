"""Task 8 (#50 follow-up): the Fresh card wires a persistent 'Not interested' (dismiss) feedback
control on its cold items (which carry a key); the other proto cards are unaffected."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import home_cards, mode_surfaces as ms
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.home_fixture_support import (
    home_control_routes, home_routes_only, publish_cards, service, without_metadata_providers,
)
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, native_sql_calls, uncontended_file_consumer_store as store, without_query_timing,
)

pytestmark = pytest.mark.usefixtures('unlocked_home_controls', 'home_control_routes',
                                    'without_metadata_providers', 'native_sql_calls', 'without_query_timing')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'),), snapshot_guards=False) as schema:
        yield schema


def _client(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0,
                                 background_work=False),
                      base_url="http://127.0.0.1")


def test_fresh_cold_item_gets_feedback_radio_does_not(store):
    # Port from /home/fresh (removed) to /home/cards. assemble_cards filters keyless items at bundle
    # assembly, so radio-fallback items (no key) never reach the card; keyed cold items get the
    # dismiss control. Seed a credible fresh bucket (>= the _MIN_CARD floor) so the card renders.
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
    ], retired_ids=[], now=1000.0)
    cold = [{"key": f"cold|{i}", "video_id": f"v{i}", "title": f"Cold Song {i}",
             "artist": f"Cold Artist {i}", "album": "", "thumbnail": None,
             "plays": 0, "reason": "New", "lane": "cold", "genre": "House"} for i in range(6)]
    payload = {"1": {surf: (cold if surf == "fresh" else []) for surf in ms.CARD_SURFACES}}
    seed_publication_subjects(store, cold, discoveries=[item['key'] for item in cold])
    publish_bundles(store, payload, 1000.0, fresh=cold)
    # Render a real assembled fresh menu. Repeated worker menu preparation is
    # below this HTTP-control boundary and has its own preparation owners.
    result = service(store).cards(home_cards.CardsRequest(1000, record=False))
    assert result.protos
    publish_cards(store, result, 1000)
    c = _client(store)
    html = c.get("/home/cards").text
    assert "Cold Song 0" in html
    assert "cold|0" in html                                    # cold item's dismiss is wired
    assert 'song-menu-trigger' in html                        # global song actions, no per-row popup
    assert 'class="rowmenu-pop"' not in html
    assert 'hx-post="/recs/feedback"' in html                  # dismiss fires from the cold items' ✕
