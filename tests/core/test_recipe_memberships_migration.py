"""Ordered recipe identity references survive upgrade, edits and restart."""
import sqlite3

import pytest

def test_membership_constraints_updates_and_atomic_replace(store):
    recipe = store.road_trip.save_road_trip_recipe(None,'Trip',50,[],['Synth Pop'],60,100)
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute("UPDATE road_trip_recipes SET selections='[]' WHERE id=?", (recipe,))
    before = store.road_trip.get_road_trip_recipe(recipe)
    with pytest.raises(ValueError,match='same genre twice'):
        store.road_trip.save_road_trip_recipe(recipe,'Changed',60,[],['Synth Pop','synthpop'],60,200)
    assert store.road_trip.get_road_trip_recipe(recipe) == before
    store.road_trip.delete_road_trip_recipe(recipe)
    assert store.road_trip.get_road_trip_recipe(recipe) is None
