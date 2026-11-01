import json
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.rec import home_prepared, journeys, mode_surfaces
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, seed_catalog_track
from tests.recommendation_support import publish_bundles, seed_bundle_subjects

pytestmark = pytest.mark.usefixtures('unlocked_home_controls')


@pytest.fixture
def store():
    s = Store(":memory:")
    try:
        s.init_schema()
        yield s
    finally:
        s.conn.close()


def _client(store, now=1000.0):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    prepare_home(store, now)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                      base_url="http://127.0.0.1")


def prepare_home(store, now=1000.0):
    """Seed eligible subjects, then prepare candidate pools before Home as the worker does."""
    if (not store.recommendations.head('home') and not store.recommendations.head('fresh')
            and store.rec_query.library_keys()):
        assert mode_surfaces.prepare_bundles(store, now) is not None
    # The worker continues bounded preparation slices. A cold slice can finish
    # its reusable inputs without yet publishing a menu, especially during
    # tuning expiry; give the fixture the same bounded continuation.
    for _ in range(3):
        if home_prepared.prepare(store, now) is not None:
            break
    else:
        raise AssertionError('Home did not publish after three preparation slices')


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
    seed_bundle_subjects(store, payload)
    publish_bundles(store, payload, 1.0)
    return payload


def test_cards_route_renders_mode_cards(store):
    _seed_bundles(store)
    r = _client(store).get("/home/cards")
    assert r.status_code == 200
    assert "Song" in r.text


def test_mix_regeneration_submits_and_closes_the_requesting_views_offer(store):
    from html import unescape
    from yt_playlist.rec import choice_evidence
    _seed_bundles(store)
    client = _client(store)
    first = client.get('/home/cards')
    match = re.search(r'hx-vals=\'([^\']*opportunity_id[^\']*)\'', first.text)
    assert match, 'Regeneration must send the ID from the rendered recipe'
    held = json.loads(unescape(match[1]))['opportunity_id']
    other = choice_evidence.present(store, 'mixes', [{'id': 'other'}], 1000,
                                   context={}, window='another-tab')
    assert client.post('/home/refresh-cards', data={'opportunity_id': held}).status_code == 200
    assert client.post('/home/refresh-cards', data={'opportunity_id': held}).status_code == 200
    assert client.post('/home/refresh-cards').status_code == 200
    closes = [row for row in store.rec_ledger.evidence().records('response') if row['kind'] == 'close']
    assert [row['opportunity_id'] for row in closes] == [held]
    assert all(row['opportunity_id'] != other for row in closes)


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
    bundles = _seed_bundles(store)
    publish_bundles(store, {
        **{mid: {"fresh": bucket["fresh"]} for mid, bucket in bundles.items() if not mid.startswith('_')},
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
    from yt_playlist.rec import stored_home
    from yt_playlist.rec.surfaces import ForYouItem
    proto = {"lane": "wheelhouse", "label": "More in your wheelhouse", "name": "Mix", "note": "",
             "tracks": [ForYouItem(f"Song {i}", f"Artist {i}", "", f"v{i}", None, 0, "reason", key=f"k{i}")
                        for i in range(4)],
             "feedback_surface": None}
    for i in range(4):
        seed_catalog_track(store, f"k{i}", f"Song {i}", f"Artist {i}", video_id=f"v{i}")
    calls = []

    def candidates(store, now, **kwargs):
        calls.append('wheelhouse')
        return [proto]

    monkeypatch.setattr(stored_home, "unmodeled_cards", candidates)
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
