"""#14 — a single grow (ring) won't pull too many tracks from the same album.

The expand route over-fetches candidates and caps how many share an album (ALBUM_CAP), so an album
can't dominate a ring. Untagged-album tracks aren't capped (each is its own thing).
"""
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


def _modelled(store, vid, title, artist, album, xy):
    store.upsert_track(vid, title, artist, album, 200, thumbnail="t.jpg")
    k = identity_key(title, artist)
    v = np.asarray(xy, dtype=np.float32)
    v /= np.linalg.norm(v) + 1e-9
    store.conn.execute("INSERT OR REPLACE INTO rec_vectors(identity_key, vec) VALUES (?,?)",
                       (k, v.tobytes()))
    store.conn.commit()
    return k


def test_ring_caps_two_per_album(store):
    c, _ = _client(store)
    kp = _modelled(store, "p", "Seed", "P", "", _unit(0))
    # five tracks all from the same album, all near the seed
    for i in range(5):
        _modelled(store, f"a{i}", f"Track {i}", "A", "Greatest Hits", _unit(2 + i))
    res = c.post("/clusters/expand", json={"pos_keys": [kp], "k": 6}).json()
    albums = [t["album"] for t in res["ring"]]
    assert albums.count("Greatest Hits") <= 2          # the album can't dominate the ring


def test_untagged_album_not_capped(store):
    c, _ = _client(store)
    kp = _modelled(store, "p", "Seed", "P", "", _unit(0))
    for i in range(5):
        _modelled(store, f"u{i}", f"Untagged {i}", f"Art{i}", "", _unit(2 + i))   # no album
    res = c.post("/clusters/expand", json={"pos_keys": [kp], "k": 6}).json()
    assert len(res["ring"]) == 5                        # all five untagged tracks allowed


def test_album_cap_is_cluster_wide_not_just_per_ring(store):
    # Two tracks from "Greatest Hits" are already on the canvas (passed as `exclude`); a further grow
    # must add ZERO more from that album — the cap spans the whole playlist, not one ring.
    c, _ = _client(store)
    kp = _modelled(store, "p", "Seed", "P", "", _unit(0))
    on1 = _modelled(store, "h0", "On 0", "A", "Greatest Hits", _unit(40))
    on2 = _modelled(store, "h1", "On 1", "A", "Greatest Hits", _unit(41))
    for i in range(4):                                  # more candidates from the same album, near seed
        _modelled(store, f"c{i}", f"Cand {i}", "A", "Greatest Hits", _unit(2 + i))
    res = c.post("/clusters/expand",
                 json={"pos_keys": [kp], "exclude": [kp, on1, on2], "k": 6}).json()
    albums = [t["album"] for t in res["ring"]]
    assert albums.count("Greatest Hits") == 0          # album already at the cap on the canvas


def test_cap_basis_excludes_central_seeds(store):
    # Seed-album tracks are on the canvas (in `exclude`) but they're central SEEDS, not grown tracks,
    # so `count_keys` omits them — they must not pre-spend the album's budget.
    c, _ = _client(store)
    kp = _modelled(store, "p", "Seed", "P", "", _unit(0))
    s1 = _modelled(store, "s1", "Seed A", "P", "Greatest Hits", _unit(40))
    s2 = _modelled(store, "s2", "Seed B", "P", "Greatest Hits", _unit(41))
    for i in range(3):
        _modelled(store, f"c{i}", f"Cand {i}", "A", "Greatest Hits", _unit(2 + i))
    res = c.post("/clusters/expand",
                 json={"pos_keys": [kp], "exclude": [kp, s1, s2], "count_keys": [], "k": 6}).json()
    albums = [t["album"] for t in res["ring"]]
    assert albums.count("Greatest Hits") == 2          # seeds didn't consume the budget; 2 fresh allowed


def test_pruned_canvas_tracks_dont_count_toward_cap(store):
    # A canvas track that's pruned (in neg_keys) won't be saved, so it must NOT consume album budget.
    c, _ = _client(store)
    kp = _modelled(store, "p", "Seed", "P", "", _unit(0))
    pruned = _modelled(store, "pr", "Pruned", "A", "Greatest Hits", _unit(40))
    for i in range(3):
        _modelled(store, f"c{i}", f"Cand {i}", "A", "Greatest Hits", _unit(2 + i))
    res = c.post("/clusters/expand",
                 json={"pos_keys": [kp], "exclude": [kp, pruned], "neg_keys": [pruned], "k": 6}).json()
    albums = [t["album"] for t in res["ring"]]
    assert albums.count("Greatest Hits") == 2          # pruned copy didn't eat into the budget


def test_album_cap_still_fills_from_other_albums(store):
    c, _ = _client(store)
    kp = _modelled(store, "p", "Seed", "P", "", _unit(0))
    for i in range(4):
        _modelled(store, f"a{i}", f"A {i}", "A", "Alb One", _unit(2 + i))
    for i in range(4):
        _modelled(store, f"b{i}", f"B {i}", "B", "Alb Two", _unit(20 + i))
    res = c.post("/clusters/expand", json={"pos_keys": [kp], "k": 6}).json()
    albums = [t["album"] for t in res["ring"]]
    assert albums.count("Alb One") <= 2 and albums.count("Alb Two") <= 2
    assert len(res["ring"]) == 4                        # 2 from each album
