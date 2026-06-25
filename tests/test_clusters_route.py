"""Clusters tab routes: the page shell, library autosuggest, and a node's push-away expansion ring."""
import json
import math

import numpy as np
from fastapi.testclient import TestClient

from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def _unit(deg):
    r = math.radians(deg)
    return (math.cos(r), math.sin(r))


def _modelled_track(store, vid, title, artist, xy):
    store.upsert_track(vid, title, artist, None, None, thumbnail="t.jpg")
    k = identity_key(title, artist)
    v = np.asarray(xy, dtype=np.float32)
    v /= np.linalg.norm(v) + 1e-9
    return k, (k, v.tobytes())


def test_page_renders_canvas(store):
    c, _ = _client(store)
    store.replace_rec_vectors([("a|b", np.ones(4, dtype=np.float32).tobytes())])   # model built
    html = c.get("/clusters").text
    assert "cluster-canvas" in html          # the pan/zoom canvas mount point


def test_page_without_model_prompts_to_sync(store):
    c, _ = _client(store)
    html = c.get("/clusters").text
    assert "cluster-canvas" not in html      # no model yet -> canvas hidden
    assert "taste model" in html.lower()


def test_search_returns_json_seeds(store):
    c, _ = _client(store)
    _, row = _modelled_track(store, "r1", "Spektrum", "Ritmo", _unit(0))
    store.replace_rec_vectors([row])
    res = c.get("/clusters/search", params={"q": "rit"}).json()
    assert any(r["kind"] == "artist" and r["label"] == "Ritmo" for r in res)


def test_expand_returns_pushed_ring(store):
    c, _ = _client(store)
    kp, rp = _modelled_track(store, "p", "Seed", "P", _unit(0))
    ka, ra = _modelled_track(store, "a", "Near A", "A", _unit(20))
    kb, rb = _modelled_track(store, "b", "Near B", "B", _unit(-20))
    kn, rn = _modelled_track(store, "n", "Pruned", "N", _unit(18))   # sits on top of A
    store.replace_rec_vectors([rp, ra, rb, rn])

    ring = c.post("/clusters/expand",
                  json={"pos_keys": [kp], "neg_keys": [kn], "exclude": [], "k": 2}).json()["ring"]
    keys = [t["key"] for t in ring]
    assert kp not in keys and kn not in keys          # seeds suppressed
    assert keys[0] == kb                              # push-away demotes A (near the pruned seed)
    assert ring[0]["title"] == "Near B"              # ring carries display metadata


def test_expand_empty_without_model(store):
    c, _ = _client(store)
    ring = c.post("/clusters/expand", json={"pos_keys": ["x|y"], "neg_keys": []}).json()["ring"]
    assert ring == []


def _save_client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient()
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), fc


def test_save_resolves_keys_to_tracks(store):
    c, fc = _save_client(store)
    store.upsert_track("va", "Ta", "Art", None, None)
    store.upsert_track("vb", "Tb", "Art", None, None)
    keep = [identity_key("Ta", "Art"), identity_key("Tb", "Art")]
    r = c.post("/clusters/save", data={"name": "My Cluster", "keep_keys": json.dumps(keep),
                                       "central_keys": "[]"})
    assert r.status_code == 200 and "Saved" in r.text
    # a cluster now saves with its own 'cluster' recipe (#15), so the title is versioned like any
    # generated mix ("My Cluster #1") and its recipe is persisted.
    assert fc.created[0][1].startswith("My Cluster")
    assert sorted(fc.added[0][1]) == ["va", "vb"]
    assert store.get_recipe(fc.created[0][0])["model"] == "cluster"


def test_save_include_central_toggles_central_tracks(store):
    c, fc = _save_client(store)
    store.upsert_track("va", "Ta", "Art", None, None)
    store.upsert_track("vc", "Tc", "Central", None, None)
    keep = json.dumps([identity_key("Ta", "Art")])
    central = json.dumps([identity_key("Tc", "Central")])

    c.post("/clusters/save", data={"name": "No Central", "keep_keys": keep, "central_keys": central})
    assert "vc" not in fc.added[0][1]                       # central excluded by default

    c.post("/clusters/save", data={"name": "With Central", "keep_keys": keep,
                                   "central_keys": central, "include_central": "on"})
    assert "vc" in fc.added[1][1]                           # checkbox folds central tracks in


def test_save_rejects_empty(store):
    c, _ = _save_client(store)
    r = c.post("/clusters/save", data={"name": "x", "keep_keys": "[]", "central_keys": "[]"})
    assert r.status_code == 200 and "Couldn't save" in r.text
