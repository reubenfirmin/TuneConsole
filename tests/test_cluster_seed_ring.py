"""Opening rings represent their artists, with repeatable variety and normal canvas constraints."""
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.rec import embed, rec_params
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app


def _track(store, title, artist, angle=0, album="", genre=""):
    tid = store.tracks.upsert_track(title, title, artist, album, 180, thumbnail="/static/nag/intro.png")
    key = identity_key(title, artist)
    if genre:
        store.tracks.set_track_genre(tid, genre)
    v = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))], dtype=np.float32)
    store.conn.execute("INSERT INTO rec_vectors(identity_key, vec) VALUES (?,?)", (key, v.tobytes()))
    store.conn.commit()
    return key


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1")


def _body(*seeds, **extra):
    return {"pos_keys": list(dict.fromkeys(k for s in seeds for k in s["keys"])),
            "central_seeds": seeds, "random_seed": 42, "k": 6, "count_keys": [], **extra}


def test_artist_songs_are_preferred_and_sampled_repeatably(store):
    own = [_track(store, f"Own {i}", "A", i * 2) for i in range(18)]
    for i in range(12):
        _track(store, f"Other {i}", "Other", 10)
    body = _body({"kind": "artist", "label": "A", "keys": own})
    c = _client(store)
    first = c.post("/clusters/expand", json=body).json()["ring"]
    assert len(first) == 6
    assert all(t["artist"] == "A" for t in first)
    assert first == c.post("/clusters/expand", json=body).json()["ring"]
    variants = {tuple(t["key"] for t in c.post("/clusters/expand", json={**body, "random_seed": seed}).json()["ring"])
                for seed in range(4)}
    assert len(variants) > 1
    # Growing again can offer more of the artist without repeating the first ring.
    second = c.post("/clusters/expand", json={**body, "exclude": [t["key"] for t in first]}).json()["ring"]
    assert len(second) == 6
    assert {t["key"] for t in first}.isdisjoint(t["key"] for t in second)


def test_multiple_artists_get_equal_room_despite_different_catalog_sizes(store):
    a = [_track(store, f"A {i}", "A", 0) for i in range(24)]
    b = [_track(store, f"B {i}", "B", 90) for i in range(6)]
    c = _client(store)
    ring = c.post("/clusters/expand", json=_body(
        {"kind": "artist", "label": "A", "keys": a},
        {"kind": "artist", "label": "B", "keys": b})).json()["ring"]
    assert [t["artist"] for t in ring].count("A") == 3
    assert [t["artist"] for t in ring].count("B") == 3


@pytest.mark.parametrize("space", ["collaborative", "content"])
def test_each_artist_offers_songs_most_like_the_other_artist(store, space):
    # Most songs establish each artist's distinct sound. The crossovers sit almost at the OTHER
    # artist, while the middle picks are nearer the combined centroid and their own artist.
    a = [_track(store, f"Shpongle core {i}", "Shpongle", 0) for i in range(8)]
    a_middle = _track(store, "Shpongle middle", "Shpongle", 30)
    a_cross = _track(store, "Synth pop crossover", "Shpongle", 80)
    b = [_track(store, f"Pet Shop Boys core {i}", "Pet Shop Boys", 90) for i in range(8)]
    b_middle = _track(store, "Pet Shop Boys middle", "Pet Shop Boys", 60)
    b_cross = _track(store, "Psychedelic crossover", "Pet Shop Boys", 10)
    a += [a_middle, a_cross]
    b += [b_middle, b_cross]
    if space == "content":
        store.rec_model.replace_rec_content_vectors(store.rec_model.get_rec_vectors())
        store.rec_model.replace_rec_vectors([(k, np.array([1, 0], dtype=np.float32).tobytes()) for k in a + b])
        rec_params.set_param(store, "cluster_content_weight", 1.0)
    else:
        rec_params.set_param(store, "cluster_content_weight", 0.0)
    body = _body({"kind": "artist", "label": "Shpongle", "keys": a},
                 {"kind": "artist", "label": "Pet Shop Boys", "keys": b}, k=2)
    c = _client(store)
    for seed in range(4):
        ring = c.post("/clusters/expand", json={**body, "random_seed": seed}).json()["ring"]
        assert {t["key"] for t in ring} == {a_cross, b_cross}
    # Artist order must not change which crossover songs are selected.
    reverse = {**body, "central_seeds": list(reversed(body["central_seeds"]))}
    assert {t["key"] for t in c.post("/clusters/expand", json=reverse).json()["ring"]} == {a_cross, b_cross}


def test_three_artists_require_a_match_to_both_other_artists(store):
    a = [_track(store, f"A core {i}", "A", 180) for i in range(12)]
    only_b = _track(store, "A matching only B", "A", -60)
    both = _track(store, "A matching B and C", "A", 0)
    a += [only_b, both]
    b = [_track(store, f"B {i}", "B", -60) for i in range(6)]
    c = [_track(store, f"C {i}", "C", 60) for i in range(6)]
    body = _body(*({"kind": "artist", "label": label, "keys": keys}
                   for label, keys in [("A", a), ("B", b), ("C", c)]), k=3)
    ring = _client(store).post("/clusters/expand", json=body).json()["ring"]
    assert {t["artist"] for t in ring} == {"A", "B", "C"}
    assert [t["key"] for t in ring if t["artist"] == "A"] == [both]


def test_seed_preference_respects_pruning_filters_explicit_songs_and_album_caps(store):
    own = [_track(store, f"Own {i}", "A", album="One Album", genre="psytrance") for i in range(7)]
    wrong_genre = _track(store, "Wrong genre", "A", genre="acid techno")
    others = [_track(store, f"Fill {i}", "Other", genre="psytrance") for i in range(6)]
    body = _body({"kind": "artist", "label": "A", "keys": own + [wrong_genre]},
                 {"kind": "song", "label": "Own 0", "keys": own[:1]},
                 exclude=[own[0], own[1]], neg_keys=[own[2]], count_keys=[own[1]],
                 allow_genres=["trance"])
    ring = _client(store).post("/clusters/expand", json=body).json()["ring"]
    keys = {t["key"] for t in ring}
    assert len(ring) == 6
    assert not keys.intersection(own[:3] + [wrong_genre])
    assert sum(t["artist"] == "A" for t in ring) == 1  # one album place already spent
    assert len(keys.intersection(others)) == 5


def test_artist_groups_have_equal_influence_on_related_picks(store):
    rec_params.set_param(store, "cluster_seed_spread", 0.0)
    a = [_track(store, f"A {i}", "A", 0) for i in range(20)]
    b = [_track(store, "B", "B", 90)]
    near_a = _track(store, "Near A", "Other", 5)
    middle = _track(store, "Middle", "Other", 45)
    near_b = _track(store, "Near B", "Other", 85)
    ranked = embed.cluster_expand(store, pos_keys=a + b, seed_groups=[a, b], topn=3)
    assert ranked[0][0] == middle
    scores = dict(ranked)
    assert abs(scores[near_a] - scores[near_b]) < 0.06
    emphasized = embed.cluster_expand(store, pos_keys=a + b + b + b, seed_groups=[a, b], topn=3)
    assert emphasized[0][0] == near_b  # explicit emphasis still steers a grouped ring
