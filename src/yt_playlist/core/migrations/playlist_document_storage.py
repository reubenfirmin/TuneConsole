"""Fold saved playlist and Road Trip selections into their existing owners."""
from .. import collection_recovery_epoch
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'fold_playlist_document_storage'
RETIRED = ('playlist_recipe_facets', 'rec_recipes', 'cluster_canvas', 'recipe_artists',
           'recipe_genres', 'recipe_unresolved_artists', 'recipe_unresolved_genres')
OWNERS = ('playlist_scopes', 'road_trip_recipes', 'tv_append_intents')
MARKER = 'uq_playlist_recipe_identity'

_HEADER = {
    'id': 'INTEGER',
    'revision': 'TEXT CHECK(length({field})=32)',
    'created_at': """REAL CHECK({field} IS NULL OR
        (typeof({field}) IN ('integer','real') AND abs({field})<=1.7976931348623157e308))""",
    'recovery_filename': 'TEXT',
    'recovery_digest': "TEXT CHECK(length({field})=64 AND {field} NOT GLOB '*[^0-9a-f]*')",
    'recovery_bytes': """INTEGER CHECK(
        ({prefix}_revision IS NULL AND {prefix}_created_at IS NULL AND {prefix}_recovery_filename IS NULL
         AND {prefix}_recovery_digest IS NULL AND {field} IS NULL)
        OR ({prefix}_revision IS NOT NULL AND {prefix}_id IS NOT NULL AND {prefix}_recovery_filename IS NOT NULL
            AND {prefix}_recovery_digest IS NOT NULL AND typeof({field})='integer' AND {field} BETWEEN 1 AND 16777216)
    )""",
}
_RECIPE = {
    'media': "TEXT CHECK({field} IN ('audio','video'))",
    'model': 'TEXT',
    'journey': 'TEXT',
    'opportunity_id': 'TEXT',
    'option_id': 'TEXT',
    'facets': """TEXT CHECK(({prefix}_revision IS NULL AND {field} IS NULL)
        OR ({prefix}_revision IS NOT NULL AND json_type({field}) IS 'array'))""",
}


def predecessor_objects(conn):
    owners = (*RETIRED, *OWNERS)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE tbl_name IN ({",".join("?" for _ in owners)}) '
        'AND sql IS NOT NULL ORDER BY type,name', owners)]


def _fold_playlist_document(conn, prefix, source):
    fields = _HEADER | (_RECIPE if prefix == 'recipe' else {})
    for field, declaration in fields.items():
        column = f'{prefix}_{field}'
        sql = declaration.format(field=column, prefix=prefix)
        conn.execute(f'ALTER TABLE playlist_scopes ADD COLUMN {column} {sql}')

    columns = ','.join(f'{prefix}_{field}' for field in fields)
    values = [f'r.{field}' for field in fields if field != 'facets']
    if prefix == 'recipe':
        values.append("""(SELECT json_group_array(json_array(f.kind,f.position,f.axis_id,f.captured_value)
                               ORDER BY f.kind,f.position)
                          FROM playlist_recipe_facets f WHERE f.recipe_id=r.id)""")
    conn.execute(f'UPDATE playlist_scopes AS p SET ({columns})=({",".join(values)}) '
                 f'FROM {source} AS r WHERE p.id=r.playlist_scope_id')
    conn.execute(f'CREATE UNIQUE INDEX uq_playlist_{prefix}_identity ON playlist_scopes({prefix}_id)')
    high_water = conn.execute(f'SELECT max(coalesce((SELECT seq FROM sqlite_sequence WHERE name=?),0),'
                              f'coalesce((SELECT max(id) FROM {source}),0))', (source,)).fetchone()[0]
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                 (f'playlist_{prefix}_id_high_water', str(high_water)))


def _fold_road_trip_selections(conn):
    conn.execute("""ALTER TABLE road_trip_recipes ADD COLUMN selections TEXT NOT NULL DEFAULT
        '{"artists":[],"genres":[]}' CHECK(json_type(selections) IS 'object'
        AND json_type(selections,'$.artists') IS 'array' AND json_type(selections,'$.genres') IS 'array')
    """)
    conn.execute("""
        WITH artist_choices(recipe_id,position,artist_id,label,reason) AS (
            SELECT m.recipe_id,m.position,m.artist_id,a.name,NULL
            FROM recipe_artists m JOIN artists a ON a.id=m.artist_id
            UNION ALL SELECT recipe_id,position,NULL,raw_label,reason FROM recipe_unresolved_artists
        ), genre_choices(recipe_id,role,position,genre_key,label,reason) AS (
            SELECT m.recipe_id,m.role,m.position,m.genre_key,g.name,NULL
            FROM recipe_genres m JOIN genre_catalog g ON g.key=m.genre_key
            UNION ALL SELECT recipe_id,role,position,NULL,raw_label,reason FROM recipe_unresolved_genres
        )
        UPDATE road_trip_recipes AS r SET selections=json_object(
            'artists', json((SELECT json_group_array(json_object(
                'position',position,'artist_id',artist_id,'label',label,'reason',reason) ORDER BY position)
                FROM artist_choices WHERE recipe_id=r.id)),
            'genres', json((SELECT json_group_array(json_object(
                'role',role,'position',position,'genre_key',genre_key,'label',label,'reason',reason) ORDER BY role,position)
                FROM genre_choices WHERE recipe_id=r.id)))
    """)


def _retarget_append_receipts(conn):
    old_reference = 'REFERENCES rec_recipes(id)'
    old_scope = 'FROM rec_recipes r WHERE r.id=NEW.recipe_id AND r.playlist_scope_id=NEW.playlist_scope_id'
    new_scope = ('FROM playlist_scopes r WHERE r.recipe_id=NEW.recipe_id AND r.id=NEW.playlist_scope_id '
                 'AND r.recipe_revision IS NOT NULL')
    declarations = conn.execute("SELECT type,sql FROM sqlite_schema WHERE tbl_name='tv_append_intents' "
                                "AND sql IS NOT NULL ORDER BY type='table' DESC,type,name").fetchall()
    conn.execute('ALTER TABLE tv_append_intents RENAME TO before_playlist_document_append_intents')
    for kind, sql in declarations:
        if kind == 'table':
            if old_reference not in sql:
                raise MigrationError('Missing TV append recipe reference')
            conn.execute(sql.replace(old_reference, 'REFERENCES playlist_scopes(recipe_id)'))
            conn.execute('INSERT INTO tv_append_intents SELECT * FROM before_playlist_document_append_intents')
            conn.execute('DROP TABLE before_playlist_document_append_intents')
        else:
            conn.execute(sql.replace(old_scope, new_scope))


def install(conn, _context):
    _fold_playlist_document(conn, 'recipe', 'rec_recipes')
    _fold_playlist_document(conn, 'canvas', 'cluster_canvas')
    conn.execute("CREATE INDEX ix_playlist_recent_video_recipes ON playlist_scopes(recipe_created_at DESC,id) "
                 "WHERE recipe_media='video' AND recipe_revision IS NOT NULL")
    _fold_road_trip_selections(conn)
    _retarget_append_receipts(conn)
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    collection_recovery_epoch.install(conn)
    collection_recovery_epoch.install_keys(conn)


def validate(conn):
    installed = objects(conn, (MARKER, *RETIRED))
    if set(installed) != {MARKER}:
        raise MigrationError('Saved playlist and Road Trip selections were not folded')
