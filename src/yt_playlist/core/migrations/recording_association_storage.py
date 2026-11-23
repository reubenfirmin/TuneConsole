"""Keep correction progress on its association and retire the Library ready flag."""
from ..association_work import MARKER, STEPS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'fold_recording_association_progress'
OWNER = 'item_recording_associations'
WORK = 'recording_association_work'
LIBRARY = 'recording_library_state'
RETIRED = (WORK, LIBRARY)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?,?) '
                                        'AND sql IS NOT NULL ORDER BY type,name', (OWNER, *RETIRED))]


def install(conn, _context):
    ready = conn.execute(f'SELECT ready FROM {LIBRARY} WHERE id=1').fetchone()
    if ready is None or ready[0] != 1:
        raise MigrationError('Recording Library membership has not been prepared')
    if conn.execute(f'SELECT 1 FROM {WORK} w JOIN {OWNER} a ON a.id=w.association_id '
                    'WHERE a.video_id<>w.video_id LIMIT 1').fetchone():
        raise MigrationError('A recording correction belongs to the wrong upload')
    conn.execute(f'ALTER TABLE {OWNER} ADD COLUMN work_plan_json TEXT')
    for step in STEPS:
        conn.execute(f'ALTER TABLE {OWNER} ADD COLUMN work_{step} INTEGER NOT NULL DEFAULT 0 CHECK(work_{step} IN (0,1))')
    columns = ','.join(('work_plan_json', *('work_' + step for step in STEPS)))
    values = ','.join(('plan_json', *STEPS))
    conn.execute(f'UPDATE {OWNER} AS a SET ({columns})=(SELECT {values} FROM {WORK} w WHERE w.association_id=a.id) '
                 f'WHERE id IN (SELECT association_id FROM {WORK})')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON {OWNER}(video_id) WHERE work_plan_json IS NOT NULL')
    conn.execute(f'DROP TABLE {WORK}')
    conn.execute(f'DROP TABLE {LIBRARY}')


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Recording correction progress was not consolidated')
