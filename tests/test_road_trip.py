"""Road Trip recipe and draft persistence."""
import pytest

from tests.road_trip_support import no_network


def test_road_trip_recipe_crud(store):
    rid = store.road_trip.save_road_trip_recipe(None, "Beach Run", 60, ["Tame Impala"], ["synthpop"],
                                       240, 1000.0, familiarity_pct=80)
    assert isinstance(rid, int)

    recipes = store.road_trip.list_road_trip_recipes()
    assert len(recipes) == 1
    r = recipes[0]
    assert r == {"id": rid, "name": "Beach Run", "own_pct": 60, "artists": ["Tame Impala"],
                "genres": ["Synth-Pop"], "target_minutes": 240,
                "blacklist_genres": [],
                "familiarity_pct": 80, "last_playlist_id": None, "created_at": 1000.0,
                "updated_at": 1000.0}

    got = store.road_trip.get_road_trip_recipe(rid)
    assert got["name"] == "Beach Run"
    assert store.road_trip.get_road_trip_recipe(rid + 1) is None

    store.road_trip.save_road_trip_recipe(rid, "Beach Run 2", 70, ["Tame Impala", "MGMT"], ["synthpop"],
                                300, 2000.0)
    updated = store.road_trip.get_road_trip_recipe(rid)
    assert updated["name"] == "Beach Run 2"
    assert updated["own_pct"] == 70
    assert updated["artists"] == ["Tame Impala", "MGMT"]
    assert updated["target_minutes"] == 300
    assert updated["familiarity_pct"] == 50           # default when the caller doesn't pass one
    assert updated["updated_at"] == 2000.0

    store.road_trip.set_road_trip_last_playlist(rid, "PL_NEW_0")
    assert store.road_trip.get_road_trip_recipe(rid)["last_playlist_id"] == "PL_NEW_0"

    store.road_trip.delete_road_trip_recipe(rid)
    assert store.road_trip.list_road_trip_recipes() == []


def test_road_trip_draft_crud_and_recipe_delete_cascade(store):
    store.accounts.upsert_account('Main','main.json',None,True)
    rid = store.road_trip.save_road_trip_recipe(None, "Beach Run", 60, [], [], 60, 1000.0)
    assert store.road_trip.get_road_trip_draft(rid) is None
    assert store.road_trip.latest_road_trip_draft() is None

    state = {"revision":"first","picks":["v1","v2"],"banned":[],"seed":7}
    store.road_trip.save_road_trip_draft(rid,state,1000.0)
    assert store.road_trip.get_road_trip_draft(rid) == state
    assert store.road_trip.latest_road_trip_draft() == {"recipe_id":rid,"state":state}

    store.road_trip.save_road_trip_draft(rid,{"revision":"second","picks":["v3"],"banned":[],"seed":8},2000.0)
    assert store.road_trip.get_road_trip_draft(rid)["picks"] == ["v3"]

    store.road_trip.delete_road_trip_recipe(rid)                # deleting the recipe takes its draft with it
    assert store.road_trip.get_road_trip_draft(rid) is None


@pytest.mark.parametrize('field,value', [
    ('own_pct', -1), ('own_pct', 101), ('own_pct', True), ('own_pct', 50.5), ('own_pct', '50'),
    ('familiarity_pct', -1), ('familiarity_pct', 101), ('familiarity_pct', False),
    ('familiarity_pct', 50.), ('familiarity_pct', None),
    ('target_minutes', 0), ('target_minutes', 14), ('target_minutes', 721),
    ('target_minutes', True), ('target_minutes', 60.5), ('target_minutes', '60'),
    ('now', float('nan')), ('now', float('inf')), ('now', -float('inf')),
    ('now', True), ('now', '100'), ('now', 10 ** 400),
])
def test_recipe_rejects_invalid_domain_values_before_insert_or_update(store, field, value):
    recipe_id = store.road_trip.save_road_trip_recipe(None, 'Original', 50, [], [], 60, 100)
    original = store.road_trip.get_road_trip_recipe(recipe_id)
    values = dict(own_pct=50, familiarity_pct=50, target_minutes=60, now=200)
    values[field] = value
    changes = store.conn.total_changes
    for target in (None, recipe_id):
        with pytest.raises(ValueError):
            store.road_trip.save_road_trip_recipe(target, 'Invalid replacement', artists=[], genres=[], **values)
        assert store.conn.total_changes == changes
        assert store.road_trip.list_road_trip_recipes() == [original]


@pytest.mark.parametrize('own_pct,familiarity_pct,target_minutes', [(0, 100, 15), (100, 0, 720)])
def test_recipe_supported_boundaries_survive_reload(store, own_pct, familiarity_pct, target_minutes):
    recipe_id = store.road_trip.save_road_trip_recipe(None, 'Boundary', own_pct, [], [], target_minutes, 100,
                                                   familiarity_pct=familiarity_pct)
    stored = store.road_trip.get_road_trip_recipe(recipe_id)
    assert (stored['own_pct'], stored['familiarity_pct'], stored['target_minutes']) == (
        own_pct, familiarity_pct, target_minutes)
