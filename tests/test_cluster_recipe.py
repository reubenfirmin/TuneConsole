"""#15: a saved cluster lands in Generated with its own tunable 'cluster' recipe.

The recipe gives the playlist a distinct type, an energy-arc journey (so the Flow lever is real),
and a 'Made from' line built from the seeds you used, the genre families you restricted to (#29),
and the genres/eras actually present. The standard generated-playlist feedback panel then applies.
"""
import json

from fastapi.testclient import TestClient

from yt_playlist.rec import recipes
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def _track(store, vid, title, artist, *, genre="", year=""):
    store.tracks.upsert_track(vid, title, artist, "Alb", 200, thumbnail="t.jpg")
    k = identity_key(title, artist)
    if genre or year:
        store.conn.execute("UPDATE tracks SET genre=?, mb_year=? WHERE identity_key=?", (genre, year, k))
        store.conn.commit()
    return k


# --- recipes.cluster_recipe ------------------------------------------------------------------

def test_cluster_recipe_shape_and_seeds(store):
    store.identities.upsert_identity("m", "c", None, True)
    a = _track(store, "v1", "Goa One", "A", genre="goa trance", year="2003")
    b = _track(store, "v2", "Goa Two", "B", genre="psytrance", year="2008")
    recipe, order = recipes.cluster_recipe(store, [a, b], seed_labels=["Ritmo"],
                                             allow_families=["trance"])
    assert recipe["model"] == "cluster"
    assert recipe["journey"] == "energy_arc"
    assert recipe["facets"]["artists"] == ["Ritmo"]
    assert recipe["facets"]["genres"] == ["trance"]        # whitelist wins
    assert "2000" in recipe["facets"]["eras"]              # derived decade present
    assert sorted(order) == sorted([a, b])                 # a permutation of the kept keys


def test_cluster_recipe_derives_genres_without_whitelist(store):
    store.identities.upsert_identity("m", "c", None, True)
    a = _track(store, "v1", "Goa One", "A", genre="goa trance")
    b = _track(store, "v2", "Goa Two", "B", genre="psytrance")
    recipe, _ = recipes.cluster_recipe(store, [a, b], seed_labels=[], allow_families=[])
    assert recipe["facets"]["genres"] == ["trance"]        # derived from the tracks present


def test_cluster_recipe_uses_chosen_journey(store):
    store.identities.upsert_identity("m", "c", None, True)
    a = _track(store, "v1", "X", "A", genre="acid techno", year="1995")
    recipe, _ = recipes.cluster_recipe(store, [a], journey="time_machine")
    assert recipe["journey"] == "time_machine"


def test_cluster_recipe_auto_journey_defaults_to_energy_arc(store):
    store.identities.upsert_identity("m", "c", None, True)
    a = _track(store, "v1", "X", "A", genre="acid techno")
    for j in ("auto", "bogus", ""):
        recipe, _ = recipes.cluster_recipe(store, [a], journey=j)
        assert recipe["journey"] == "energy_arc"


def test_save_passes_journey_through(store):
    c, iid = _client(store)
    a = _track(store, "v1", "Goa One", "A", genre="goa trance", year="2003")
    b = _track(store, "v2", "Goa Two", "B", genre="psytrance", year="2008")
    form = {"name": "Mix", "keep_keys": json.dumps([a, b]), "central_keys": "[]",
            "journey": "deep_dive"}
    r = c.post("/clusters/save", data=form)
    assert r.status_code == 200
    from yt_playlist.repos.rec_query import GENERATED_GROUP
    ytm = next(y for y, g in store.playlists.get_playlist_groups().items() if g == GENERATED_GROUP)
    assert store.rec_surface.get_recipe(ytm)["journey"] == "deep_dive"


def test_cluster_recipe_remembers_inputs_in_params(store):
    store.identities.upsert_identity("m", "c", None, True)
    a = _track(store, "v1", "X", "A", genre="acid techno")
    recipe, _ = recipes.cluster_recipe(store, [a], seed_labels=["A"], allow_families=["techno"])
    assert recipe["params"]["seeds"] == ["A"]
    assert recipe["params"]["genre_whitelist"] == ["techno"]


def test_cluster_recipe_shuffle_spaces_artists_for_track_keys(store):
    keys = [_track(store, f"v{artist}{i}", f"Song {i}", artist, genre="techno")
            for artist, count in [("A", 3), ("B", 2), ("C", 2)] for i in range(count)]
    _, order = recipes.cluster_recipe(store, keys, journey="shuffle")
    meta = store.rec_query.tracks_by_keys(keys)
    assert sorted(order) == sorted(keys)
    assert all(meta[a]["artist"] != meta[b]["artist"] for a, b in zip(order, order[1:]))
    assert order == recipes.cluster_recipe(store, keys, journey="shuffle")[1]


# --- /clusters/save persists the recipe --------------------------------------------------------

def test_save_tags_cluster_recipe(store):
    c, iid = _client(store)
    a = _track(store, "v1", "Goa One", "A", genre="goa trance", year="2003")
    b = _track(store, "v2", "Goa Two", "B", genre="psytrance", year="2008")
    form = {"name": "Car mix", "keep_keys": json.dumps([a, b]), "central_keys": "[]",
            "seed_labels": json.dumps(["Ritmo"]), "allow_families": json.dumps(["trance"])}
    r = c.post("/clusters/save", data=form)
    assert r.status_code == 200

    from yt_playlist.repos.rec_query import GENERATED_GROUP
    groups = store.playlists.get_playlist_groups()
    ytm = next(y for y, g in groups.items() if g == GENERATED_GROUP)
    recipe = store.rec_surface.get_recipe(ytm)
    assert recipe is not None
    assert recipe["model"] == "cluster"
    assert recipe["facets"]["artists"] == ["Ritmo"]
    assert recipe["journey"] == "energy_arc"


def test_save_without_seeds_or_whitelist_still_tags_cluster(store):
    c, iid = _client(store)
    a = _track(store, "v1", "X", "A", genre="acid techno")
    b = _track(store, "v2", "Y", "B", genre="detroit techno")
    form = {"name": "Mix", "keep_keys": json.dumps([a, b]), "central_keys": "[]"}
    r = c.post("/clusters/save", data=form)
    assert r.status_code == 200
    from yt_playlist.repos.rec_query import GENERATED_GROUP
    ytm = next(y for y, g in store.playlists.get_playlist_groups().items() if g == GENERATED_GROUP)
    assert store.rec_surface.get_recipe(ytm)["model"] == "cluster"
