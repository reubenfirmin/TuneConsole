"""#15: a saved cluster lands in Generated with its own tunable 'cluster' recipe.

The recipe gives the playlist a distinct type, an energy-arc journey (so the Flow lever is real),
and a 'Made from' line built from the seeds you used, the genre families you restricted to (#29),
and the genres/eras actually present. The standard generated-playlist feedback panel then applies.
"""
import json
from tests.enrichment_support import seed_enrichment

import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import recipes
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.home_fixture_support import without_metadata_providers
from tests.playlist_fixture_support import library_catalog
from tests.repository_mock_support import no_database, repository_store
from tests.store_fixture_support import (
    file_consumer_store as cluster_store, native_sql_calls, without_query_timing,
)
from tests.web_consumer_support import install_routes


@pytest.fixture
def recipe_store(no_database):
    return repository_store(
        values={'_read_only': True},
        rec_query={'track_genres': {}, 'track_decades': {}, 'track_last_played': {},
                   'play_counts': {}, 'tracks_by_keys': {}},
        genres={'feature_axes': {'goa trance': ('trance', 'goa-trance'),
                                 'psytrance': ('trance', 'psytrance'),
                                 'acid techno': ('techno', 'acid-techno'),
                                 'techno': ('techno', None)}},
        musical_profiles={'by_keys': {}},
    )


def _recipe_tracks(store, rows):
    keys = [identity_key(title, artist) for title, artist, _, _ in rows]
    store.rec_query.track_genres.return_value = {
        key: genre for key, (_, _, genre, _) in zip(keys, rows) if genre}
    store.rec_query.track_decades.return_value = {
        key: str(int(year) // 10 * 10)
        for key, (_, _, _, year) in zip(keys, rows) if year}
    store.rec_query.tracks_by_keys.return_value = {
        key: {'artist': artist} for key, (_, artist, _, _) in zip(keys, rows)}
    return keys


def _client(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient(catalog=library_catalog(store))}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def _track(store, vid, title, artist, *, genre="", year=""):
    entry = store.tracks.upsert_track(vid, title, artist, "Alb", 200, thumbnail="t.jpg")
    k = identity_key(title, artist)
    if genre or year:
        seed_enrichment(store, entry, genre, year)
    return k


# --- recipes.cluster_recipe ------------------------------------------------------------------

def test_cluster_recipe_shape_and_seeds(recipe_store):
    store = recipe_store
    a, b = _recipe_tracks(store, [('Goa One', 'A', 'goa trance', '2003'),
                                  ('Goa Two', 'B', 'psytrance', '2008')])
    recipe, order = recipes.cluster_recipe(store, [a, b], seed_labels=["Ritmo"],
                                             allow_families=["trance"])
    assert recipe["model"] == "cluster"
    assert recipe["journey"] == "energy_arc"
    assert recipe["facets"]["artists"] == ["Ritmo"]
    assert recipe["facets"]["genres"] == ["trance"]        # whitelist wins
    assert "2000" in recipe["facets"]["eras"]              # derived decade present
    assert sorted(order) == sorted([a, b])                 # a permutation of the kept keys


def test_cluster_recipe_derives_genres_without_whitelist(recipe_store):
    store = recipe_store
    a, b = _recipe_tracks(store, [('Goa One', 'A', 'goa trance', ''),
                                  ('Goa Two', 'B', 'psytrance', '')])
    recipe, _ = recipes.cluster_recipe(store, [a, b], seed_labels=[], allow_families=[])
    assert recipe["facets"]["genres"] == ["trance"]        # derived from the tracks present


def test_cluster_recipe_uses_chosen_journey(recipe_store):
    store = recipe_store
    a, = _recipe_tracks(store, [('X', 'A', 'acid techno', '1995')])
    recipe, _ = recipes.cluster_recipe(store, [a], journey="time_machine")
    assert recipe["journey"] == "time_machine"


def test_cluster_recipe_auto_journey_defaults_to_energy_arc(recipe_store):
    store = recipe_store
    a, = _recipe_tracks(store, [('X', 'A', 'acid techno', '')])
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
    from yt_playlist.repos.base import GENERATED_GROUP
    ytm = next(y for y, g in store.playlists.get_playlist_groups().items() if g == GENERATED_GROUP)
    assert store.playlist_recipes.get_recipe(ytm)["journey"] == "deep_dive"


def test_cluster_recipe_remembers_inputs_in_params(recipe_store):
    store = recipe_store
    a, = _recipe_tracks(store, [('X', 'A', 'acid techno', '')])
    recipe, _ = recipes.cluster_recipe(store, [a], seed_labels=["A"], allow_families=["techno"])
    assert recipe["params"]["seeds"] == ["A"]
    assert recipe["params"]["genre_whitelist"] == ["techno"]


def test_cluster_recipe_shuffle_spaces_artists_for_track_keys(recipe_store):
    store = recipe_store
    keys = _recipe_tracks(store, [(f'Song {i}', artist, 'techno', '')
                                 for artist, count in [('A', 3), ('B', 2), ('C', 2)]
                                 for i in range(count)])
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

    from yt_playlist.repos.base import GENERATED_GROUP
    groups = store.playlists.get_playlist_groups()
    ytm = next(y for y, g in groups.items() if g == GENERATED_GROUP)
    recipe = store.playlist_recipes.get_recipe(ytm)
    assert recipe is not None
    assert recipe["model"] == "cluster"
    assert recipe["facets"]["artists"] == ["Ritmo"]
    assert recipe["journey"] == "energy_arc"


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing', 'without_metadata_providers')
def test_save_without_seeds_or_whitelist_still_tags_cluster(cluster_store, monkeypatch):
    store = cluster_store
    install_routes(monkeypatch, 'clusters')
    c, iid = _client(store)
    a = _track(store, "v1", "X", "A", genre="acid techno")
    b = _track(store, "v2", "Y", "B", genre="detroit techno")
    form = {"name": "Mix", "keep_keys": json.dumps([a, b]), "central_keys": "[]"}
    r = c.post("/clusters/save", data=form)
    assert r.status_code == 200
    from yt_playlist.repos.base import GENERATED_GROUP
    ytm = next(y for y, g in store.playlists.get_playlist_groups().items() if g == GENERATED_GROUP)
    assert store.playlist_recipes.get_recipe(ytm)["model"] == "cluster"
