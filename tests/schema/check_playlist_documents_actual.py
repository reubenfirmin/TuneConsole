"""Compare the saved playlist and Road Trip data on a private C175 library."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core.migrations.playlist_document_storage import RETIRED
from yt_playlist.core.store import Store


def _rows(conn, sql):
    return list(map(tuple, conn.execute(sql)))


def _tables(conn):
    return {name for name, in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_playlist_document_upgrade():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == 175
        before = _tables(conn)
        headers = _rows(conn, '''SELECT playlist_scope_id,id,revision,created_at,media,model,journey,
            opportunity_id,option_id,recovery_filename,recovery_digest,recovery_bytes FROM rec_recipes ORDER BY id''')
        canvases = _rows(conn, '''SELECT playlist_scope_id,id,revision,created_at,recovery_filename,recovery_digest,
            recovery_bytes FROM cluster_canvas ORDER BY id''')
        facets = _rows(conn, '''SELECT recipe_id,kind,position,axis_id,captured_value
            FROM playlist_recipe_facets ORDER BY recipe_id,kind,position''')
        artists = _rows(conn, '''SELECT m.recipe_id,m.position,m.artist_id,a.name,NULL
            FROM recipe_artists m JOIN artists a ON a.id=m.artist_id
            UNION ALL SELECT recipe_id,position,NULL,raw_label,reason FROM recipe_unresolved_artists
            ORDER BY 1,2''')
        genres = _rows(conn, '''SELECT m.recipe_id,m.role,m.position,m.genre_key,g.name,NULL
            FROM recipe_genres m JOIN genre_catalog g ON g.key=m.genre_key
            UNION ALL SELECT recipe_id,role,position,NULL,raw_label,reason FROM recipe_unresolved_genres
            ORDER BY 1,2,3''')
        receipts = _rows(conn, 'SELECT * FROM tv_append_intents ORDER BY id')
        assert headers and canvases and facets and artists and genres

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        after = _tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        assert headers == _rows(conn, '''SELECT id,recipe_id,recipe_revision,recipe_created_at,recipe_media,
            recipe_model,recipe_journey,recipe_opportunity_id,recipe_option_id,recipe_recovery_filename,
            recipe_recovery_digest,recipe_recovery_bytes FROM playlist_scopes
            WHERE recipe_revision IS NOT NULL ORDER BY recipe_id''')
        assert canvases == _rows(conn, '''SELECT id,canvas_id,canvas_revision,canvas_created_at,canvas_recovery_filename,
            canvas_recovery_digest,canvas_recovery_bytes FROM playlist_scopes
            WHERE canvas_revision IS NOT NULL ORDER BY canvas_id''')
        assert facets == _rows(conn, '''SELECT p.recipe_id,f.value->>0,f.value->>1,f.value->>2,f.value->>3
            FROM playlist_scopes p,json_each(p.recipe_facets) f ORDER BY p.recipe_id,f.key''')
        assert artists == _rows(conn, '''SELECT r.id,a.value->>'$.position',a.value->>'$.artist_id',
            a.value->>'$.label',a.value->>'$.reason'
            FROM road_trip_recipes r,json_each(r.selections,'$.artists') a ORDER BY r.id,a.key''')
        assert genres == _rows(conn, '''SELECT r.id,g.value->>'$.role',g.value->>'$.position',g.value->>'$.genre_key',
            g.value->>'$.label',g.value->>'$.reason'
            FROM road_trip_recipes r,json_each(r.selections,'$.genres') g ORDER BY r.id,g.key''')
        assert receipts == _rows(conn, 'SELECT * FROM tv_append_intents ORDER BY id')
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        scope = headers[0][0]
        saved = store.playlist_recipes.get_recipe(scope)
        assert isinstance(saved, dict)
        store.playlist_recipes.set_recipe(scope, {**saved, 'model': 'Edited in upgrade check'}, 4)
        canvas = canvases[0][0]
        assert isinstance(store.playlist_recipes.get_cluster_canvas(canvas), str)
        store.playlist_recipes.set_cluster_canvas(canvas, 'Edited canvas', 4)
        recipe = store.road_trip.get_road_trip_recipe(artists[0][0])
        assert recipe['artists'] == [row[3] for row in artists if row[0] == recipe['id']]
        store.road_trip.save_road_trip_recipe(recipe['id'], 'Edited Road Trip', recipe['own_pct'],
            recipe['artists'], recipe['genres'], recipe['target_minutes'], 4,
            recipe['familiarity_pct'], recipe['blacklist_genres'])

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert store.playlist_recipes.get_recipe(scope)['model'] == 'Edited in upgrade check'
        assert store.playlist_recipes.get_cluster_canvas(canvas) == 'Edited canvas'
        assert store.road_trip.get_road_trip_recipe(recipe['id'])['name'] == 'Edited Road Trip'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before - after),
                  exact_playlist_recipes=len(headers), exact_canvases=len(canvases), exact_facets=len(facets),
                  exact_road_trip_artist_choices=len(artists), exact_road_trip_genre_choices=len(genres),
                  source_append_receipts=len(receipts), opened_and_edited_after_upgrade=True, reopened=True,
                  foreign_key_errors=0)
    report_file('playlist-documents-c179.json').write_text(json.dumps(report, indent=2) + '\n')
