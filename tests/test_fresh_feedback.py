"""Task 8 (#50 follow-up): the Fresh card wires a persistent 'Not interested' (dismiss) feedback
control on its cold items (which carry a key); radio-fallback items (no key) do not get it, and the
other proto cards are unaffected."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1")


def test_fresh_cold_item_gets_feedback_radio_does_not(store):
    c = _client(store)
    store.put_proposals("fresh_songs", [
        {"video_id": "v1", "title": "Cold Song", "artist": "A", "thumbnail": None,
         "key": "cold|a", "reason": "New", "lane": "cold"},
        {"video_id": "v2", "title": "Radio Song", "artist": "B", "thumbnail": None},   # no key
    ], 1000.0)
    html = c.get("/home/fresh").text
    assert "Cold Song" in html and "Radio Song" in html
    assert "cold|a" in html                                    # cold item's dismiss is wired
    assert html.count('class="dots"') == 1                     # one "..." kebab (the cold item only)
    assert html.count('hx-post="/recs/feedback"') == 1         # dismiss lives inside that kebab's popup
    assert 'class="rowmenu-pop"' in html                       # reuses the regular-song feedback menu UI
