"""Task 8 (#50 follow-up): the Fresh card wires a persistent 'Not interested' (dismiss) feedback
control on its cold items (which carry a key); the other proto cards are unaffected."""
import numpy as np
from fastapi.testclient import TestClient

from yt_playlist.rec import mode_surfaces as ms
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
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
             "plays": 0, "reason": "New", "lane": "cold", "genre": ""} for i in range(6)]
    payload = {"1": {surf: (cold if surf == "fresh" else []) for surf in ms.CARD_SURFACES}}
    store.put_proposals("mode_bundles", payload, 1000.0)
    c = _client(store)
    html = c.get("/home/cards").text
    assert "Cold Song 0" in html
    assert "cold|0" in html                                    # cold item's dismiss is wired
    assert 'class="dots"' not in html                          # kebab menu removed (merged into the ✕)
    assert 'class="rowmenu-pop"' not in html                   # popup gone
    assert 'hx-post="/recs/feedback"' in html                  # dismiss fires from the cold items' ✕
