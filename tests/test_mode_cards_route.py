import json
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.rec import home_prepared, journeys, mode_surfaces
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, seed_catalog_track


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _client(store, now=1000.0):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    prepare_home(store, now)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def prepare_home(store, now=1000.0):
    """Seed eligible subjects, then prepare candidate pools before Home as the worker does."""
    payload = store.rec_proposals.get_proposals("mode_bundles") or {}
    tracks = {item["key"]: (lane, item) for mid, bucket in payload.items() if not mid.startswith("_")
              for lane, items in bucket.items() for item in items if item.get("key")}
    owned = store.rec_query.tracks_by_keys(tracks)
    discovered = store.discovery.discovered_tracks_by_keys(tracks)
    for key, (lane, item) in tracks.items():
        if key in owned or key in discovered:
            continue
        if 'title' not in item or 'artist' not in item:
            continue  # genre-option-only fixture rows are never playable candidates
        video = "fixture-" + key
        if lane == "fresh":
            store.discovery.upsert_discovered_track(key, video, item["title"], item["artist"], "", None,
                                                   item.get("genre"), None, None, 1)
        else:
            seed_catalog_track(store, key, item["title"], item["artist"], video_id=video,
                               genre=item.get("genre"))
    if (not payload and not store.rec_proposals.get_proposals("fresh_pool")
            and store.rec_query.library_keys()):
        assert mode_surfaces.prepare_bundles(store, now) is not None
    assert home_prepared.prepare(store, now) is not None


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
    store.rec_proposals.put_proposals("mode_bundles", payload, 1.0)


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
        if rec["model"] == "mode":
            assert rec.get("mode_id") is not None
            assert rec.get("lane") in {"wheelhouse", "explore", "comfort", "fresh", "temporal"}
        else:
            assert rec["model"] == "fresh"  # a thin menu can add a prepared Fresh fallback
        assert rec.get("journey") in journeys.JOURNEYS


def test_fresh_mode_card_recipe_retains_lane(store):
    _seed_bundles(store)
    bundles = store.rec_proposals.get_proposals("mode_bundles")
    store.rec_proposals.put_proposals("mode_bundles", {
        **{mid: {"fresh": bucket["fresh"]} for mid, bucket in bundles.items()},
        "_meta": {"full_pools": True},
    }, 1)
    response = _client(store).get("/home/cards")
    assert response.status_code == 200
    recipe = json.loads(re.search(r"data-recipe='([^']*)'", response.text)[1])
    assert recipe["model"] == "mode" and recipe["lane"] == "fresh"


def test_cards_route_ok_when_empty(store):
    r = _client(store).get("/home/cards")
    assert r.status_code == 200      # falls back / empty, never 500


def test_explicit_tuning_with_object_tracks_is_cached_without_request_fallback(store, monkeypatch):
    # Explicit tuning can still produce ForYouItem objects. Ordinary reads must use the
    # prepared result instead of invoking the legacy ranker when mode bundles are missing.
    from yt_playlist.rec import home_cards as home_mod
    from yt_playlist.rec.surfaces import ForYouItem
    proto = {"lane": "wheelhouse", "label": "More in your wheelhouse", "name": "Mix", "note": "",
             "tracks": [ForYouItem(f"Song {i}", f"Artist {i}", "", f"v{i}", None, 0, "reason", key=f"k{i}")
                        for i in range(4)],
             "feedback_surface": None}
    for i in range(4):
        seed_catalog_track(store, f"k{i}", f"Song {i}", f"Artist {i}", video_id=f"v{i}")
    calls = []

    def legacy_card(store, card, now, **kwargs):
        calls.append(card)
        return proto if card == "wheelhouse" else None

    monkeypatch.setattr(home_mod, "_one_card", legacy_card)
    client = _client(store)
    calls.clear()  # Background preparation may read the existing Fresh pool.
    assert client.get("/home/cards").status_code == 200
    assert calls == []
    r = client.post("/home/breadth", data={"breadth_bias": "0.5"})
    assert r.status_code == 200
    assert "Song" in r.text and "Mix" in r.text
    assert "wheelhouse" in calls
    tuning_calls = calls.copy()
    r = client.get("/home/cards")
    assert r.status_code == 200
    assert "Song" in r.text and "Mix" in r.text
    assert calls == tuning_calls
