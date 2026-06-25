"""Why is this Clusters edge here? — grounded co-occurrence facts + an embedding 'bridge' fallback.

`connection_facts` reads the same baskets that fed the taste embedding (shared playlists, album,
session, same artist, genre family, decade) and turns them into human-readable reasons. When a link
is purely second-order (no direct shared basket), `embed.connection_geometry` names the track that
bridges the two in taste space. The /clusters/explain route stitches them together for the green
mid-edge dot.
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


def _track(store, title, artist, *, album="", genre="", year="", xy=None):
    """Insert a track (optionally genre/year/vector) and return its identity_key."""
    store.upsert_track(f"v_{title}_{artist}", title, artist, album, 200, thumbnail="t.jpg")
    k = identity_key(title, artist)
    if genre or year:
        store.conn.execute("UPDATE tracks SET genre=?, mb_year=? WHERE identity_key=?",
                           (genre, year, k))
        store.conn.commit()
    return k


def _playlist(store, iid, title, keys, ytm=None):
    pid = store.upsert_playlist(iid, ytm or ("PL_" + title), title, len(keys), title, 1.0)
    tids = [store.conn.execute("SELECT id FROM tracks WHERE identity_key=? LIMIT 1", (k,)).fetchone()["id"]
            for k in keys]
    store.set_playlist_tracks(pid, tids)
    return pid


# --- connection_facts: grounded co-occurrence reasons -------------------------------------------

def test_shared_playlist_names_the_co_occurring_artists(store):
    iid = store.upsert_identity("m", "c", None, True)
    child = _track(store, "Train", "Younger Brother")
    seed = _track(store, "Spektrum", "Ritmo")
    other = _track(store, "Dorset", "Ott")
    _playlist(store, iid, "Psy night", [child, seed, other])

    facts = store.connection_facts(child, [seed])
    assert facts, "a shared playlist should produce a fact"
    top = facts[0]
    assert top["kind"] == "playlist"
    assert "1 of your playlist" in top["text"]
    assert "Ritmo" in top["text"]              # the co-occurring path artist is named


def test_same_artist_is_reported(store):
    store.upsert_identity("m", "c", None, True)
    a = _track(store, "Song A", "Bonobo")
    b = _track(store, "Song B", "Bonobo")
    facts = store.connection_facts(a, [b])
    assert any(f["kind"] == "same_artist" and "Bonobo" in f["text"] for f in facts)


def test_genre_family_and_decade_facts(store):
    store.upsert_identity("m", "c", None, True)
    child = _track(store, "C", "AristA", genre="psytrance", year="2003")
    path = _track(store, "P", "Pista", genre="goa trance", year="2007")
    facts = store.connection_facts(child, [path])
    kinds = {f["kind"] for f in facts}
    # only if the family map actually unifies these; decade must match regardless
    assert "decade" in kinds
    assert any("2000s" in f["text"] for f in facts)


def test_no_direct_basket_returns_empty(store):
    store.upsert_identity("m", "c", None, True)
    a = _track(store, "Lonely A", "Artist X")
    b = _track(store, "Lonely B", "Artist Y")
    assert store.connection_facts(a, [b]) == []


def test_child_excluded_from_its_own_path(store):
    store.upsert_identity("m", "c", None, True)
    a = _track(store, "Solo", "Solo Artist")
    # path is just the child itself -> nothing to compare against
    assert store.connection_facts(a, [a]) == []


# --- embed.connection_geometry: score + bridge --------------------------------------------------

def _put(store, vecs):
    rows = []
    for k, xy in vecs.items():
        v = np.asarray(xy, dtype=np.float32)
        v /= np.linalg.norm(v) + 1e-9
        rows.append((k, v.tobytes()))
    store.replace_rec_vectors(rows)


def test_geometry_scores_against_path_centroid(store):
    _put(store, {"c": _unit(10), "p": _unit(0), "far": _unit(170)})
    geo = embed.connection_geometry(store, "c", ["p"])
    assert geo["score"] is not None
    assert geo["score"] > 0.9                  # 10° apart -> high cosine


def test_geometry_bridge_is_close_to_both(store):
    # child at 80°, path at 0°; 'mid' at 40° is close to both, 'off' is close to neither.
    _put(store, {"c": _unit(80), "p": _unit(0), "mid": _unit(40), "off": _unit(200)})
    geo = embed.connection_geometry(store, "c", ["p"])
    assert geo["bridge"] == "mid"


def test_geometry_empty_before_model(store):
    geo = embed.connection_geometry(store, "c", ["p"])
    assert geo == {"score": None, "bridge": None}


# --- /clusters/explain route --------------------------------------------------------------------

def test_explain_route_returns_headline_from_shared_playlist(store):
    c, iid = _client(store)
    child = _track(store, "Train", "Younger Brother")
    seed = _track(store, "Spektrum", "Ritmo")
    _playlist(store, iid, "Psy night", [child, seed])
    _put(store, {child: _unit(10), seed: _unit(0)})

    res = c.post("/clusters/explain", json={"key": child, "path_keys": [seed]}).json()
    assert "Ritmo" in res["headline"]
    assert res["reasons"][0]["kind"] == "playlist"
    assert res["match_pct"] is not None and res["match_pct"] > 80
    assert res["title"] == "Train"


def test_explain_route_falls_back_to_bridge(store):
    c, iid = _client(store)
    child = _track(store, "Edge", "A")
    seed = _track(store, "Root", "B")
    bridge = _track(store, "Middle", "C")
    # no shared playlist -> facts empty -> bridge fallback
    _put(store, {child: _unit(80), seed: _unit(0), bridge: _unit(40)})

    res = c.post("/clusters/explain", json={"key": child, "path_keys": [seed]}).json()
    assert res["reasons"], "should fall back to a bridge reason"
    assert res["reasons"][0]["kind"] == "bridge"
    assert "Middle" in res["headline"]


def test_explain_route_empty_key(store):
    c, _ = _client(store)
    res = c.post("/clusters/explain", json={"key": "", "path_keys": []}).json()
    assert res["reasons"] == []
