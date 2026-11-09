"""Dashboard surfaces: fresh songs as a proto-playlist and graphical new-artist cards."""
from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only, prepared_client, publish_cards, without_metadata_providers
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store, native_sql_calls
from tests.test_mode_cards_route import prepare_home
from tests.recommendation_support import publish_bundles, seed_publication_subjects

pytestmark = pytest.mark.usefixtures('native_sql_calls', 'unlocked_home_controls', 'home_routes_only', 'without_metadata_providers')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        yield schema


def _client(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                           base_url="http://127.0.0.1")


def test_fresh_renders_as_saveable_proto(store):
    import numpy as np
    from yt_playlist.rec import home_cards
    from yt_playlist.rec import mode_surfaces as ms
    # Seed mode_bundles with a fresh-surface item; /home/cards is now the card-row endpoint
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    fresh = [{"key": "new|newcomer", "video_id": "v1", "title": "New One",
              "artist": "Newcomer", "album": "", "thumbnail": None,
              "plays": 0, "reason": "", "lane": "cold", "genre": "House"}]
    fresh += [{"key": f"more|{i}", "video_id": f"vm{i}", "title": f"More {i}", "artist": f"Artist {i}",
               "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "cold", "genre": "House"}
              for i in range(5)]                    # enough to clear the _MIN_CARD floor
    # only the fresh surface has material, so it wins the mode
    payload = {"1": {surf: (fresh if surf == "fresh" else []) for surf in ms.CARD_SURFACES}}
    seed_publication_subjects(store, fresh, discoveries=[track['key'] for track in fresh])
    publish_bundles(store, payload, 1.0, fresh=fresh)
    cards = ms.assemble_cards(store, 1, 0)
    assert len(cards) == 1 and cards[0]['lane'] == 'fresh'
    proto = home_cards._complete_card(store, cards[0], 1, 0, None, None)
    result = home_cards.CardsResult([proto], [{'proto': proto, 'pool': None}],
                                   None, None, [], [], 0, None)
    publish_cards(store, result, 1)
    with prepared_client(store, now=1, background_work=False) as client:
        html = client.get("/home/cards").text
    assert 'id="gen-fresh"' in html                 # rendered as a proto-playlist card
    assert "Fresh songs -" in html                  # dated name
    assert "Play this mix" in html                   # same save flow as the other lanes
    assert "New One" in html


def test_new_artists_render_with_thumbnail(store):
    _iid, c = _client(store)
    store.discovery.upsert_discovered_artist("Donato Dozzy", 1.0, ["Recondite"], ["Deep Focus"],
                                  "https://img/dozzy.jpg", now=1.0)
    prepare_home(store, 1.0)
    html = c.get("/home/new-artists").text
    assert "https://img/dozzy.jpg" in html          # graphical card uses the artist image
    assert "Donato Dozzy" in html
