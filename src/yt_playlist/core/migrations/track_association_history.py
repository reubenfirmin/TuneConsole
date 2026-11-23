"""Keep correction evidence and unfinished work on the affected upload."""
from ..association_work import MARKER, PENDING_ID, SERIAL, STEPS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from ..worker_settings import save

NAME = 'keep_recording_association_history_on_uploads'
RETIRED = 'item_recording_associations'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND (tbl_name=? OR name=?) ORDER BY type,name', (RETIRED, 'track_sources'))]


def install(conn, _context):
    save(conn, SERIAL, str(conn.execute('SELECT COALESCE(max(id),0) FROM item_recording_associations').fetchone()[0]))
    conn.execute("ALTER TABLE track_sources ADD COLUMN association_history TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(association_history)='array')")
    conn.execute("ALTER TABLE track_sources ADD COLUMN association_work TEXT "
                 "CHECK(association_work IS NULL OR (json_type(association_work)='object' "
                 "AND json_type(association_work,'$.id') IS 'integer'))")
    steps = ''.join(f",'{step}',work_{step}" for step in STEPS)
    # Render exact double-precision timestamps before the JSON aggregate.
    conn.execute(f'''
        WITH ordered AS (
            SELECT * FROM item_recording_associations ORDER BY video_id,id
        ), history AS (
            SELECT video_id,
                json_group_array(json_array(id,recording_id,previous_recording_id,source,evidence,
                    json(CASE WHEN at IS NOT NULL THEN printf('%!.26g',at) END))) AS entries
            FROM ordered GROUP BY video_id
        ), pending AS (
            SELECT video_id,json_object('id',id,'plan_json',work_plan_json{steps}) AS work
            FROM item_recording_associations WHERE work_plan_json IS NOT NULL
        )
        UPDATE track_sources AS s SET association_history=history.entries,association_work=pending.work
        FROM history LEFT JOIN pending USING(video_id) WHERE s.video_id=history.video_id
    ''')
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON track_sources(({PENDING_ID})) WHERE association_work IS NOT NULL')


def validate(conn):
    if set(definitions(conn, (MARKER, RETIRED))) != {MARKER}:
        raise MigrationError('Recording association evidence was not folded into track sources')
