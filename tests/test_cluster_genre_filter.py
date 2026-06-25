"""#29 — a cluster can be restricted to a whitelist of genre families (e.g. a calm car playlist).

Expansion only offers tracks whose genre FAMILY is in the chosen set; untagged tracks are dropped
while a filter is active (strict — a track with no genre can't be vouched safe).
"""
import math

import numpy as np
from fastapi.testclient import TestClient

from yt_playlist.rec import embed
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


def _track(store, title, artist, *, genre="", xy=None):
    store.upsert_track(f"v_{title}", title, artist, "", 200, thumbnail="t.jpg")
    k = identity_key(title, artist)
    if genre:
        store.conn.execute("UPDATE tracks SET genre=? WHERE identity_key=?", (genre, k))
        store.conn.commit()
    if xy is not None:
        v = np.asarray(xy, dtype=np.float32)
        v /= np.linalg.norm(v) + 1e-9
        store.conn.execute("INSERT OR REPLACE INTO rec_vectors(identity_key, vec) VALUES (?,?)",
                           (k, v.tobytes()))
        store.conn.commit()
    return k


# --- keys_in_families / library_genre_families -------------------------------------------------

def test_keys_in_families_filters_by_family_and_drops_untagged(store):
    store.upsert_identity("m", "c", None, True)
    a = _track(store, "Goa One", "A", genre="goa trance")     # -> trance family
    b = _track(store, "Techno One", "B", genre="acid techno")  # -> techno family
    _track(store, "Untagged", "C")                             # no genre -> never included
    keys = store.keys_in_families(["trance"])
    assert a in keys and b not in keys
    assert all("Untagged" not in k for k in keys)


def test_library_genre_families_lists_present_families_with_counts(store):
    store.upsert_identity("m", "c", None, True)
    _track(store, "Goa One", "A", genre="goa trance")
    _track(store, "Goa Two", "B", genre="psytrance")
    _track(store, "Techno", "C", genre="acid techno")
    fams = {f["family"]: f["n"] for f in store.library_genre_families()}
    assert fams.get("trance") == 2
    assert fams.get("techno") == 1


# --- cluster_expand allow= ---------------------------------------------------------------------

def test_cluster_expand_allow_restricts_candidates(store):
    kp = _track(store, "Seed", "P", genre="goa trance", xy=_unit(0))
    ka = _track(store, "Near Trance", "A", genre="psytrance", xy=_unit(10))
    kb = _track(store, "Near Techno", "B", genre="acid techno", xy=_unit(12))
    allow = store.keys_in_families(["trance"])
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
    assert "goa trance" in subs and "psytrance" in subs
    assert all(g["family"] == "trance" for g in res["genres"])     # carries its family


def test_keys_in_genre_selection_matches_genre_or_family(store):
    store.upsert_identity("m", "c", None, True)
    goa = _track(store, "Goa", "A", genre="goa trance")
    psy = _track(store, "Psy", "B", genre="psytrance")
    tek = _track(store, "Tek", "C", genre="acid techno")
    # a specific sub-genre token matches only that genre
    assert store.keys_in_genre_selection(["goa trance"]) == {goa}
    # a family token matches every genre in the family
    assert store.keys_in_genre_selection(["trance"]) == {goa, psy}
    # mixed tokens union
    assert store.keys_in_genre_selection(["goa trance", "techno"]) == {goa, tek}


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
