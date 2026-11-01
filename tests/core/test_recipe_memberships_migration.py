"""Ordered recipe identity references survive upgrade, edits and restart."""
import sqlite3

import pytest

from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.store import Store


def test_upgrade_preserves_order_unknowns_and_identity_edits(tmp_path):
    path = tmp_path / 'recipes.db'
    store = Store(path)
    try:
        migrate(store.conn,registry=registry.MIGRATIONS[:88])
        store._bind_repositories()
        store.tracks.upsert_track('known','Song','Known artist',None,120)
        recipe = store.road_trip.save_road_trip_recipe(None,'Trip',50,
            ['Unknown artist','Known artist'],['Synth Pop','Unverified old label'],60,100,
            blacklist_genres=['Drum And Bass'])
        original = tuple(store.conn.execute(
            'SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=?',
            (recipe,)).fetchone())
        history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))
        migrate(store.conn)
        store._bind_repositories()
        assert {'artists','genres','blacklist_genres'} <= {
            row[1] for row in store.conn.execute('PRAGMA table_info(road_trip_recipes)')}
        assert tuple(store.conn.execute(
            'SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=?',
            (recipe,)).fetchone()) == original
        assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations'))) == history
        legacy = store.road_trip.get_road_trip_recipe(recipe)
        assert legacy['artists'] == ['Unknown artist','Known artist']
        assert legacy['genres'] == ['Synth Pop','Unverified old label']
        assert legacy['blacklist_genres'] == ['Drum And Bass']
        # Admission leaves the supported legacy writer usable before preparation
        # or an explicit historical conversion has run.
        compatible = store.road_trip.save_road_trip_recipe(
            None,'New trip',50,['Another unknown'],['Jazz'],60,101)
        assert store.road_trip.get_road_trip_recipe(compatible)['artists'] == ['Another unknown']
        store.road_trip.save_road_trip_recipe(
            compatible,'Changed trip',60,['Known artist'],['Synth Pop'],90,102)
        assert store.road_trip.get_road_trip_recipe(compatible)['genres'] == ['Synth Pop']
        store.road_trip.delete_road_trip_recipe(compatible)
        assert store.road_trip.get_road_trip_recipe(compatible) is None
        assert store.road_trip.get_road_trip_recipe(recipe) == legacy
        assert tuple(store.conn.execute(
            'SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=?',
            (recipe,)).fetchone()) == original
        migrate(store.conn,registry=registry.MIGRATIONS[:89])
        migrate(store.conn,registry=registry.MIGRATIONS)
        store._bind_repositories()
        assert 'artists' not in {row[1] for row in store.conn.execute('PRAGMA table_info(road_trip_recipes)')}
        assert store.road_trip.get_road_trip_recipe(recipe)['artists'] == ['Unknown artist','Known artist']
        assert store.conn.execute('SELECT position FROM recipe_artists').fetchone()[0] == 1
        assert store.conn.execute('SELECT raw_label,position FROM recipe_unresolved_artists').fetchone()[:] == ('Unknown artist',0)
        assert store.conn.execute('SELECT raw_label,position FROM recipe_unresolved_genres').fetchone()[:] == ('Unverified old label',1)
        assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))[:len(history)] == history
        store.genres.rename_genre('synth pop','A renamed genre')
        assert store.road_trip.get_road_trip_recipe(recipe)['genres'] == ['A renamed genre','Unverified old label']
        store.genres.merge_genres('synth pop','jazz',now=200)
        assert store.road_trip.get_road_trip_recipe(recipe)['genres'] == ['Jazz','Unverified old label']
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()
    reopened = Store(path)
    try:
        migrate(reopened.conn)
        reopened._bind_repositories()
        assert reopened.road_trip.get_road_trip_recipe(recipe)['artists'] == ['Unknown artist','Known artist']
        assert reopened.road_trip.get_road_trip_recipe(recipe)['genres'] == ['Jazz','Unverified old label']
        assert reopened.road_trip.get_road_trip_recipe(recipe)['blacklist_genres'] == ['Drum & Bass']
        current.validate(reopened.conn)
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


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
