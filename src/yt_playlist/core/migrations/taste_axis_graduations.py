"""Keep the audited weight changes with the taste facet they affected."""
from .. import taste_axes, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_graduation_history_on_taste_axes'
RETIRED = 'recommendation_gradient_events'
COLUMN = (f"{taste_axes.GRADUATION_HISTORY} TEXT NOT NULL DEFAULT '[]' "
          f"CHECK(json_type({taste_axes.GRADUATION_HISTORY})='array')")


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('''SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL
        AND (name='taste_axes' OR tbl_name=?) ORDER BY type,name''', (RETIRED,))]


def install(conn, _context):
    if conn.execute(f'''SELECT 1 FROM {RETIRED} WHERE typeof(score)<>'real' OR typeof(factor)<>'real'
        OR typeof(new_weight)<>'real' OR typeof(created_at)<>'real' LIMIT 1''').fetchone():
        raise MigrationError('Unexpected value type in the installed graduation history')
    column = taste_axes.GRADUATION_HISTORY
    conn.execute(f'ALTER TABLE taste_axes ADD COLUMN {COLUMN}')
    event = taste_axes.graduation_event_sql('id', 'source', 'score', 'factor', 'new_weight', 'created_at')
    conn.execute(f'''WITH attributed AS (
        SELECT axis_id,json_group_array({event} ORDER BY id) AS events FROM {RETIRED} GROUP BY axis_id
        ) UPDATE taste_axes SET {column}=attributed.events FROM attributed WHERE taste_axes.id=attributed.axis_id''')
    high_water = conn.execute(f'''SELECT max(COALESCE((SELECT max(id) FROM {RETIRED}),0),
        COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0))''', (RETIRED,)).fetchone()[0]
    if high_water:
        worker_settings.save(conn, taste_axes.GRADUATION_SERIAL, str(high_water))
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(taste_axes.graduation_view())


def validate(conn):
    if set(definitions(conn, (RETIRED, taste_axes.GRADUATIONS))) != {taste_axes.GRADUATIONS}:
        raise MigrationError('Graduation history was not moved to its taste axes')
