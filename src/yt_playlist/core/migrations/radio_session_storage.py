"""Keep the selected checkpoint and pending playlist edit on the On Air session."""
from ..radio_checkpoint_policy import OPERATION_FIELDS
from ..schema_contract import MigrationError, table_clauses, tokens
from ..schema_inspection import definitions


NAME = 'fold_radio_session_state'
MARKER = 'uq_radio_selected_session'
RETIRED = ('radio_operation_items', 'radio_operations', 'radio_session_head')
SELECTED = 'INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1))'
INDEX = f'CREATE UNIQUE INDEX {MARKER} ON radio_sessions(selected) WHERE selected=1'
OPERATION = '''TEXT CONSTRAINT radio_operation_revision CHECK (
    operation_json IS NULL OR (
        json_valid(operation_json)
        AND json_type(operation_json,'$.session_revision') IS 'integer'
        AND json_extract(operation_json,'$.session_revision') IS revision
        AND json_extract(operation_json,'$.kind') IS NOT NULL
        AND json_extract(operation_json,'$.kind') IN ('append','replacement')
        AND json_type(operation_json,'$.lists') IS 'array'
    )
)'''


def install(conn, _context):
    conn.execute(f'ALTER TABLE radio_sessions ADD COLUMN selected {SELECTED}')
    conn.execute(f'ALTER TABLE radio_sessions ADD COLUMN operation_json {OPERATION}')
    conn.execute('UPDATE radio_sessions SET selected=1 WHERE id=(SELECT session_id FROM radio_session_head)')
    conn.execute(INDEX)
    fields = ','.join(f"'{field}',o.{field}" for field in OPERATION_FIELDS)
    conn.execute(f'''
        UPDATE radio_sessions
        SET operation_json=json_object({fields},'session_revision',o.session_revision,'lists',json((
            SELECT json_group_array(json(list)) FROM (
                SELECT json_array(role,list_position,json_group_array(video_id_snapshot)) AS list
                FROM (
                    SELECT role,list_position,video_id_snapshot FROM radio_operation_items
                    WHERE session_id=o.session_id ORDER BY role,list_position,position
                )
                GROUP BY role,list_position ORDER BY role,list_position
            )
        )))
        FROM radio_operations o
        WHERE radio_sessions.id=o.session_id
    ''')
    for name in RETIRED:
        conn.execute(f'DROP TABLE {name}')


def validate(conn):
    actual = definitions(conn, (*RETIRED, MARKER, 'radio_sessions'))
    columns = table_clauses(tokens(actual.get('radio_sessions'))) if 'radio_sessions' in actual else ()
    expected = {tokens('selected ' + SELECTED), tokens('operation_json ' + OPERATION)}
    if (actual.keys() & set(RETIRED) or tokens(actual.get(MARKER)) != tokens(INDEX)
            or not expected.issubset(columns)):
        raise MigrationError('On Air session state was not folded')
