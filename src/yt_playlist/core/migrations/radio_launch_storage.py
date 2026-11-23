"""Keep On Air reservations and restart recovery on the episode they belong to."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_radio_launch_state'
TABLE = 'radio_sessions'
INDEX = 'uq_radio_seeded_recovery'
FILENAME = 'recovery_filename'
RECEIPT_PREFIX = 'radio-launch:'
RECOVERY_KEY = 'radio_seeded_launch_recovery'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? '
                                        'AND sql IS NOT NULL ORDER BY type,name', (TABLE,))]


def _with_reservations(sql):
    for name in ('account_id', 'revision', FILENAME, 'recovery_digest', 'recovery_bytes', 'updated_at'):
        at = sql.index(' NOT NULL', sql.index('\n ' + name + ' '))
        sql = sql[:at] + sql[at:].removeprefix(' NOT NULL')
    for name in ('revision', 'recovery_bytes', 'updated_at'):
        sql = sql.replace(f'CHECK(typeof({name})', f'CHECK({name} IS NULL OR typeof({name})', 1)
    sql = sql.replace("status IN ('preparing'", "status IN ('reserved','preparing'")
    return sql.rstrip().removesuffix(')') + ''',
 launch_ids TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(launch_ids) AND json_type(launch_ids)='array'),
 seeded_recovery TEXT CHECK(seeded_recovery IS NULL OR (
   json_valid(seeded_recovery) AND json_type(seeded_recovery)='object'
   AND json_extract(seeded_recovery,'$.episode_id') IS id
   AND json_type(seeded_recovery,'$.launch_id') IS 'text')),
 CHECK (
   (status='reserved' AND revision IS NULL AND recovery_filename IS NULL AND recovery_digest IS NULL
    AND recovery_bytes IS NULL AND playlist_scope_id IS NULL AND selected=0 AND operation_json IS NULL
    AND updated_at IS NULL)
   OR (status<>'reserved' AND account_id IS NOT NULL AND revision IS NOT NULL
       AND recovery_filename IS NOT NULL AND recovery_digest IS NOT NULL
       AND recovery_bytes IS NOT NULL AND updated_at IS NOT NULL)
 )
)'''


def install(conn, _context):
    table, *attached = sorted(predecessor_objects(conn), key=lambda sql: not sql.startswith('CREATE TABLE'))
    conn.execute('ALTER TABLE radio_sessions RENAME TO previous_radio_sessions')
    conn.execute(_with_reservations(table))
    columns = ('id,account_id,playlist_scope_id,status,revision,recovery_filename,recovery_digest,'
               'recovery_bytes,updated_at,selected,operation_json')
    conn.execute(f'INSERT INTO radio_sessions({columns}) SELECT {columns} FROM previous_radio_sessions')
    conn.execute('DROP TABLE previous_radio_sessions')
    for sql in attached:
        conn.execute(sql)
    conn.execute(f'CREATE UNIQUE INDEX {INDEX} ON radio_sessions((1)) WHERE seeded_recovery IS NOT NULL')
    conn.execute("""
        INSERT INTO radio_sessions(id,status,launch_ids)
        SELECT value,'reserved',json_group_array(substr(key,?) ORDER BY key)
        FROM settings WHERE key GLOB 'radio-launch:*' GROUP BY value
        ON CONFLICT(id) DO UPDATE SET launch_ids=excluded.launch_ids
    """, (len(RECEIPT_PREFIX) + 1,))
    conn.execute("""
        INSERT INTO radio_sessions(id,status,seeded_recovery)
        SELECT json_extract(value,'$.episode_id'),'reserved',value FROM settings WHERE key=?
        ON CONFLICT(id) DO UPDATE SET seeded_recovery=excluded.seeded_recovery
    """, (RECOVERY_KEY,))
    conn.execute("DELETE FROM settings WHERE key GLOB 'radio-launch:*' OR key=?", (RECOVERY_KEY,))


def validate(conn):
    schema = definitions(conn, (TABLE, INDEX))
    if INDEX not in schema or 'seeded_recovery' not in tokens(schema.get(TABLE)):
        raise MigrationError('On Air sessions are missing launch recovery storage')
