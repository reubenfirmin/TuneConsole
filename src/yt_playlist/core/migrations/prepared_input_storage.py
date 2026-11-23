"""Keep recording and unresolved inputs in one typed preparation table."""
from .. import prepared_input_schema, prepared_inputs
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'own_prepared_inputs_in_one_table'
TABLE = 'prepared_recording_inputs'
RETIRED = 'unresolved_preparation_inputs'
OWNERS = (TABLE, RETIRED)
VIEWS = tuple(view for _table, view, _owner in prepared_input_schema.RELATIONS)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND tbl_name IN (?,?,?,?) ORDER BY type,name', (*OWNERS, *VIEWS))]


def install(conn, _context):
    original = definitions(conn, (TABLE,))[TABLE]
    table = original.replace('recording_id INTEGER PRIMARY KEY NOT NULL REFERENCES recordings(id),',
        "recording_id INTEGER UNIQUE REFERENCES recordings(id),raw_key TEXT UNIQUE,"
        "reason TEXT CHECK(reason IN ('empty_retraction','missing_owner','retired_owner')),")
    table = table.removesuffix(') WITHOUT ROWID') + (
        ',CHECK((recording_id IS NULL) = (raw_key IS NOT NULL)),CHECK((raw_key IS NULL) = (reason IS NULL)))')
    guards = conn.execute('SELECT name,sql FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL '
        "AND type IN ('index','trigger') ORDER BY name", (TABLE,)).fetchall()
    for view in VIEWS:
        conn.execute(f'DROP VIEW {view}')
    _replace_table(conn, TABLE, table, expressions={'raw_key': 'NULL', 'reason': 'NULL'})
    columns = ','.join(('raw_key', 'reason', *prepared_inputs.COLUMNS))
    conn.execute(f'INSERT INTO {TABLE}({columns}) SELECT {columns} FROM {RETIRED}')
    conn.execute(f'DROP TABLE {RETIRED}')
    for name, sql in guards:
        if name.startswith('group_clock_'):
            row = 'OLD' if name.endswith('_delete') else 'NEW'
            sql = sql.replace(' BEGIN ', f' WHEN {row}.recording_id IS NOT NULL BEGIN ')
        conn.execute(sql)


def validate(conn):
    if definitions(conn, (RETIRED, *VIEWS)):
        raise MigrationError('Retired prepared-input storage remains')
