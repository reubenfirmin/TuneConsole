"""Ordered recipe identity references survive upgrade, edits and restart."""
import sqlite3

import pytest

def test_membership_constraints_updates_and_atomic_replace(store):
    recipe = store.road_trip.save_road_trip_recipe(None,'Trip',50,[],['Synth Pop'],60,100)
    with store.conn:
        store.conn.execute("INSERT INTO recipe_unresolved_genres VALUES (?,'include',1,'Old unknown','unknown_genre')",(recipe,))
    for sql,values in (
        ("INSERT INTO recipe_genres VALUES (?,'absent','include',2)",(recipe,)),
        ("INSERT INTO recipe_genres VALUES (?,'jazz','other',2)",(recipe,)),
        ("UPDATE recipe_genres SET position=1 WHERE recipe_id=?",(recipe,)),
        ("UPDATE recipe_unresolved_genres SET position=0 WHERE recipe_id=?",(recipe,)),
    ):
        with pytest.raises(sqlite3.IntegrityError), store.conn:
            store.conn.execute(sql,values)
    before = store.road_trip.get_road_trip_recipe(recipe)
    with pytest.raises(ValueError,match='same genre twice'):
        store.road_trip.save_road_trip_recipe(recipe,'Changed',60,[],['Synth Pop','synthpop'],60,200)
    assert store.road_trip.get_road_trip_recipe(recipe) == before
    store.road_trip.delete_road_trip_recipe(recipe)
    assert not store.conn.execute('SELECT 1 FROM recipe_genres').fetchone()
    assert not store.conn.execute('SELECT 1 FROM recipe_unresolved_genres').fetchone()
