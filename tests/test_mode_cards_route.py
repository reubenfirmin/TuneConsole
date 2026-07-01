import json
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.rec import journeys
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _client(store, now=1000.0):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def _seed_bundles(store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
        {"mode_id": 2, "label": "b", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 60, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    from yt_playlist.rec import mode_surfaces as ms
    def items(p, g):
        return [{"key": f"{p}{i}", "video_id": "v", "title": f"Song {p}{i}", "artist": f"Art {p}{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": g}
                for i in range(20)]
    payload = {"1": {}, "2": {}}
    for surf in ms.CARD_SURFACES:
        payload["1"][surf] = items(f"{surf}h", "house")
        payload["2"][surf] = items(f"{surf}t", "techno")
    store.put_proposals("mode_bundles", payload, 1.0)


def test_cards_route_renders_mode_cards(store):
    _seed_bundles(store)
    r = _client(store).get("/home/cards")
    assert r.status_code == 200
    assert "Song" in r.text


def test_mode_card_recipe_carries_a_journey(store):
    # A saved mode card must record a DJ journey in its recipe so the playlist page can show + tune
    # the Flow lever (parity with the per-lane _carded cards). Regression: the mode-card recipe was
    # just {"model": "mode", "mode_id": N} with no journey, so the saved playlist showed no Flow.
    _seed_bundles(store)
    r = _client(store).get("/home/cards")
    assert r.status_code == 200
    recipes = [json.loads(m.replace("&#34;", '"').replace("&quot;", '"'))
               for m in re.findall(r"data-recipe='([^']*)'", r.text)]
    assert recipes, "no mode cards rendered"
    for rec in recipes:
        assert rec["model"] == "mode" and rec.get("mode_id") is not None
        assert rec.get("journey") in journeys.JOURNEYS


def test_cards_route_ok_when_empty(store):
    r = _client(store).get("/home/cards")
    assert r.status_code == 200      # falls back / empty, never 500


def test_cards_fallback_with_object_tracks_does_not_500(store, monkeypatch):
    # Regression: with no mode_bundles the route falls back to _one_card, whose tracks are ForYouItem
    # OBJECTS (not dicts). The offered-count loop must read .key from either shape, not 500 the row.
    from yt_playlist.web.routes import home as home_mod
    from yt_playlist.rec.surfaces import ForYouItem
    proto = {"lane": "wheelhouse", "label": "More in your wheelhouse", "name": "Mix", "note": "",
             "tracks": [ForYouItem("Song", "Artist", "", "v1", None, 0, "reason", key="k1")],
             "feedback_surface": None}
    monkeypatch.setattr(home_mod, "_one_card",
                        lambda store, card, now: proto if card == "wheelhouse" else None)
    r = _client(store).get("/home/cards")
    assert r.status_code == 200
    assert "Song" in r.text and "Mix" in r.text    # fallback card rendered, no 500 on object tracks
