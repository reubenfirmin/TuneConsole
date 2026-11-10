"""Opening rings represent their artists, with repeatable variety and normal canvas constraints."""
import math
from contextlib import ExitStack

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.genre_catalog_support import bound_shipped_catalog
from tests.home_fixture_support import without_metadata_providers
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, native_sql_calls
from yt_playlist.rec import embed, rec_params
from yt_playlist.core import embeddings
from yt_playlist.core import genre_identity
from yt_playlist.core.store import Store
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app


@pytest.fixture(scope='module')
def ring_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def store(ring_schema, monkeypatch):
    # Preserve the actual aliases and parent families used by pruning. Full
    # vocabulary curation is owned by test_genre_catalog, below this consumer.
    bound_shipped_catalog(monkeypatch, ('psytrance', 'acid techno', 'trance', 'techno', 'electronic'))
    database = Store(':memory:')
    try:
        initialize_consumer(database, ring_schema)
        with database.conn:
            genre_identity.seed_catalog(database.conn)
        yield database
    finally:
        database.conn.close()


@pytest.fixture(autouse=True)
def cluster_routes_only(monkeypatch, without_metadata_providers, native_sql_calls):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, clusters

    # Keep HTTP decoding and the actual expansion route without constructing
    # unrelated library pages and provider tag matching for each ring request.
    # Genre pruning still reads the actual catalog and its family membership.
    # Serial ranking consumers keep native SQL below diagnostic/handle admission;
    # those contracts belong to test_db_diagnostics and test_db_thread_serialization.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [app_update.build(ctx), clusters.build(ctx)])


def _track(store, title, artist, angle=0, album="", genre=""):
    return _tracks(store, [title], artist, angle, album, genre)[0]


def _tracks(store, titles, artist, angle=0, album="", genre=""):
    items = [dict(key=identity_key(title, artist), video_id=title, title=title,
                  artist=artist, album=album, genre=genre or None, duration_s=180)
             for title in titles]
    seed_publication_subjects(store, items)
    v = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))], dtype=np.float32)
    with store.conn:
        store.conn.executemany('UPDATE youtube_items SET thumbnail=? WHERE video_id=?',
                               [('/static/nag/intro.png', item['video_id']) for item in items])
        embeddings.publish(store.conn, 'rec_vectors', [(item['key'], v.tobytes()) for item in items],
                           model_version='test-ring')
    return [item['key'] for item in items]


@pytest.fixture
def cluster_client():
    with ExitStack() as clients:
        def open_client(store):
            iid = store.accounts.upsert_account("main", "cred", None, True)
            app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
            # One real application lifecycle and portal per client, rather than
            # starting and stopping an event loop for every expansion request.
            return clients.enter_context(TestClient(app, base_url="http://127.0.0.1",
                backend_options={'use_uvloop': True}))

        yield open_client


def _body(*seeds, **extra):
    return {"pos_keys": list(dict.fromkeys(k for s in seeds for k in s["keys"])),
            "central_seeds": seeds, "random_seed": 42, "k": 6, "count_keys": [], **extra}


def test_artist_songs_are_preferred_and_sampled_repeatably(store, cluster_client):
    own = [_track(store, f"Own {i}", "A", i * 2) for i in range(18)]
    for i in range(12):
        _track(store, f"Other {i}", "Other", 10)
    body = _body({"kind": "artist", "label": "A", "keys": own})
    c = cluster_client(store)
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


def test_multiple_artists_get_equal_room_despite_different_catalog_sizes(store, cluster_client):
    a = _tracks(store, [f"A {i}" for i in range(24)], "A", 0)
    b = _tracks(store, [f"B {i}" for i in range(6)], "B", 90)
    c = cluster_client(store)
    ring = c.post("/clusters/expand", json=_body(
        {"kind": "artist", "label": "A", "keys": a},
        {"kind": "artist", "label": "B", "keys": b})).json()["ring"]
    assert [t["artist"] for t in ring].count("A") == 3
    assert [t["artist"] for t in ring].count("B") == 3


@pytest.mark.parametrize("space", ["collaborative", "content"])
def test_each_artist_offers_songs_most_like_the_other_artist(store, space, cluster_client):
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
    c = cluster_client(store)
    for seed in range(4):
        ring = c.post("/clusters/expand", json={**body, "random_seed": seed}).json()["ring"]
        assert {t["key"] for t in ring} == {a_cross, b_cross}
    # Artist order must not change which crossover songs are selected.
    reverse = {**body, "central_seeds": list(reversed(body["central_seeds"]))}
    assert {t["key"] for t in c.post("/clusters/expand", json=reverse).json()["ring"]} == {a_cross, b_cross}


def test_three_artists_require_a_match_to_both_other_artists(store, cluster_client):
    a = [_track(store, f"A core {i}", "A", 180) for i in range(12)]
    only_b = _track(store, "A matching only B", "A", -60)
    both = _track(store, "A matching B and C", "A", 0)
    a += [only_b, both]
    b = [_track(store, f"B {i}", "B", -60) for i in range(6)]
    c = [_track(store, f"C {i}", "C", 60) for i in range(6)]
    body = _body(*({"kind": "artist", "label": label, "keys": keys}
                   for label, keys in [("A", a), ("B", b), ("C", c)]), k=3)
    ring = cluster_client(store).post("/clusters/expand", json=body).json()["ring"]
    assert {t["artist"] for t in ring} == {"A", "B", "C"}
    assert [t["key"] for t in ring if t["artist"] == "A"] == [both]


def test_seed_preference_respects_pruning_filters_explicit_songs_and_album_caps(store, cluster_client):
    own = [_track(store, f"Own {i}", "A", album="One Album", genre="psytrance") for i in range(7)]
    wrong_genre = _track(store, "Wrong genre", "A", genre="acid techno")
    others = [_track(store, f"Fill {i}", "Other", genre="psytrance") for i in range(6)]
    body = _body({"kind": "artist", "label": "A", "keys": own + [wrong_genre]},
                 {"kind": "song", "label": "Own 0", "keys": own[:1]},
                 exclude=[own[0], own[1]], neg_keys=[own[2]], count_keys=[own[1]],
                 allow_genres=["trance"])
    ring = cluster_client(store).post("/clusters/expand", json=body).json()["ring"]
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
