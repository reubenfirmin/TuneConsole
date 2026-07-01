"""Dashboard surfaces: fresh songs as a proto-playlist and graphical new-artist cards."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                           base_url="http://127.0.0.1")


def test_fresh_renders_as_saveable_proto(store):
    import numpy as np
    from yt_playlist.rec import mode_surfaces as ms
    _iid, c = _client(store)
    # Seed mode_bundles with a fresh-surface item; /home/cards is now the card-row endpoint
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    fresh = [{"key": "new|newcomer", "video_id": "v1", "title": "New One",
              "artist": "Newcomer", "album": "", "thumbnail": None,
              "plays": 0, "reason": "", "lane": "cold", "genre": ""}]
    fresh += [{"key": f"more|{i}", "video_id": f"vm{i}", "title": f"More {i}", "artist": f"Artist {i}",
               "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "cold", "genre": ""}
              for i in range(5)]                    # enough to clear the _MIN_CARD floor
    # only the fresh surface has material, so it wins the mode
    payload = {"1": {surf: (fresh if surf == "fresh" else []) for surf in ms.CARD_SURFACES}}
    store.put_proposals("mode_bundles", payload, 1.0)
    html = c.get("/home/cards").text
    assert 'id="gen-fresh"' in html                 # rendered as a proto-playlist card
    assert "Fresh songs -" in html                  # dated name
    assert "Save &amp; play on YouTube" in html     # same save flow as the other lanes
    assert "New One" in html


def test_new_artists_render_with_thumbnail(store):
    _iid, c = _client(store)
    store.upsert_discovered_artist("Donato Dozzy", 1.0, ["Recondite"], ["Deep Focus"],
                                  "https://img/dozzy.jpg", now=1.0)
    html = c.get("/home/new-artists").text
    assert "https://img/dozzy.jpg" in html          # graphical card uses the artist image
    assert "Donato Dozzy" in html
