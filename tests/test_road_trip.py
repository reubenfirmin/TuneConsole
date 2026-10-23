"""Road Trip recipe and draft persistence."""
from tests.road_trip_support import no_network


def test_road_trip_recipe_crud(store):
    rid = store.road_trip.save_road_trip_recipe(None, "Beach Run", 60, ["Tame Impala"], ["synthpop"],
                                       240, 1000.0, familiarity_pct=80)
    assert isinstance(rid, int)

    recipes = store.road_trip.list_road_trip_recipes()
    assert len(recipes) == 1
    r = recipes[0]
    assert r == {"id": rid, "name": "Beach Run", "own_pct": 60, "artists": ["Tame Impala"],
                "genres": ["synthpop"], "target_minutes": 240,
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
    rid = store.road_trip.save_road_trip_recipe(None, "Beach Run", 60, [], [], 60, 1000.0)
    assert store.road_trip.get_road_trip_draft(rid) is None
    assert store.road_trip.latest_road_trip_draft() is None

    store.road_trip.save_road_trip_draft(rid, {"picks": ["v1", "v2"], "seed": 7}, 1000.0)
    assert store.road_trip.get_road_trip_draft(rid) == {"picks": ["v1", "v2"], "seed": 7}
    assert store.road_trip.latest_road_trip_draft() == {"recipe_id": rid,
                                              "state": {"picks": ["v1", "v2"], "seed": 7}}

    store.road_trip.save_road_trip_draft(rid, {"picks": ["v3"], "seed": 8}, 2000.0)     # upsert, not insert
    assert store.road_trip.get_road_trip_draft(rid)["picks"] == ["v3"]

    store.road_trip.delete_road_trip_recipe(rid)                # deleting the recipe takes its draft with it
    assert store.road_trip.get_road_trip_draft(rid) is None
