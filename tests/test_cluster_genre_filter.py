"""#29: a cluster can be restricted to a whitelist of genre families (e.g. a calm car playlist).

Expansion only offers tracks whose genre FAMILY is in the chosen set; untagged tracks are dropped
while a filter is active (strict: a track with no genre can't be vouched safe).
"""
import math
from tests.enrichment_support import seed_enrichment

import numpy as np
from fastapi.testclient import TestClient

from yt_playlist.core import embeddings
from yt_playlist.rec import embed
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def _unit(deg):
    r = math.radians(deg)
    return (math.cos(r), math.sin(r))


def _track(store, title, artist, *, genre="", xy=None):
    entry = store.tracks.upsert_track(f"v_{title}", title, artist, "", 200, thumbnail="t.jpg")
    k = identity_key(title, artist)
    if genre:
        seed_enrichment(store, entry, genre, None)
    if xy is not None:
        v = np.asarray(xy, dtype=np.float32)
        v /= np.linalg.norm(v) + 1e-9
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            embeddings.publish(store.conn, 'rec_vectors', [(k, v.tobytes())], model_version='genre-filter-fixture')
    return k


# --- cluster_expand allow= ---------------------------------------------------------------------

def test_cluster_expand_allow_restricts_candidates(store):
    kp = _track(store, "Seed", "P", genre="goa trance", xy=_unit(0))
    ka = _track(store, "Near Trance", "A", genre="psytrance", xy=_unit(10))
    kb = _track(store, "Near Techno", "B", genre="acid techno", xy=_unit(12))
    allow = store.rec_query.keys_in_genre_selection(["trance"])
    out = [k for k, _ in embed.cluster_expand(store, pos_keys=[kp], allow=allow, topn=5)]
    assert ka in out                      # trance candidate kept
    assert kb not in out                  # techno candidate filtered out
    assert kp not in out                  # seed still excluded


# --- routes -------------------------------------------------------------------------------------

def test_genres_route_lists_families(store):
    c, _ = _client(store)
    _track(store, "Goa", "A", genre="goa trance")
    res = c.get("/clusters/genres").json()
    assert any(f["family"] == "trance" for f in res["families"])


# --- C2b: sub-genres are selectable too ---------------------------------------------------------

def test_genres_route_lists_subgenres(store):
    c, _ = _client(store)
    _track(store, "Goa", "A", genre="goa trance")
    _track(store, "Psy", "B", genre="psytrance")
    res = c.get("/clusters/genres").json()
    subs = {g["genre"] for g in res["genres"]}
    assert "goa trance" in {s.lower() for s in subs} and "psytrance" in {s.lower() for s in subs}
    assert all(g["family"] == "trance" for g in res["genres"])     # carries its family


def test_expand_route_respects_allow_genres_subgenre(store):
    c, _ = _client(store)
    kp = _track(store, "Seed", "P", genre="goa trance", xy=_unit(0))
    ka = _track(store, "Goa Near", "A", genre="goa trance", xy=_unit(10))
    kb = _track(store, "Psy Near", "B", genre="psytrance", xy=_unit(12))
    res = c.post("/clusters/expand", json={"pos_keys": [kp], "allow_genres": ["goa trance"], "k": 5}).json()
    keys = [t["key"] for t in res["ring"]]
    assert ka in keys and kb not in keys              # only the exact sub-genre survives


def test_expand_route_respects_allow_families(store):
    c, _ = _client(store)
    kp = _track(store, "Seed", "P", genre="goa trance", xy=_unit(0))
    ka = _track(store, "Near Trance", "A", genre="psytrance", xy=_unit(10))
    kb = _track(store, "Near Techno", "B", genre="acid techno", xy=_unit(12))
    res = c.post("/clusters/expand", json={"pos_keys": [kp], "allow_families": ["trance"], "k": 5}).json()
    keys = [t["key"] for t in res["ring"]]
    assert ka in keys and kb not in keys
