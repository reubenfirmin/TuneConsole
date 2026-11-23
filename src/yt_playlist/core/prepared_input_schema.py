"""Admit the same prepared-input writes on installed and energy-free layouts."""
from . import prepared_inputs
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions as installed_definitions


RELATIONS = (
    ('prepared_recording_inputs', prepared_inputs.RECORDING_WRITES, ('recording_id',)),
    ('unresolved_preparation_inputs', prepared_inputs.UNRESOLVED_WRITES, ('raw_key', 'reason')),
)
RETIRED_COLUMNS = ('library_energy', 'discovered_energy')


def definitions(legacy_tables=()):
    for table, view, owner in RELATIONS:
        columns = (*owner, *prepared_inputs.COLUMNS)
        names = ','.join(columns)
        yield view, f'CREATE VIEW {view} AS SELECT {names} FROM {table}'
        values = ','.join(f'NEW.{column}' for column in columns)
        updates = [f'{column}=excluded.{column}' for column in columns[1:]]
        if table in legacy_tables:
            # Clear both derived values in the same write as their mask bits.
            updates.extend(f'{column}=NULL' for column in RETIRED_COLUMNS)
        trigger = view + '_insert'
        yield trigger, (f'CREATE TRIGGER {trigger} INSTEAD OF INSERT ON {view} BEGIN '
                        f'INSERT INTO {table}({names}) VALUES ({values}) ON CONFLICT({owner[0]}) '
                        f'DO UPDATE SET {",".join(updates)}; END')


def _admitted_definitions(conn):
    legacy = set()
    retired_columns = set(RETIRED_COLUMNS)
    for table, _view, _owner in RELATIONS:
        columns = {row[1] for row in conn.execute(f'PRAGMA main.table_info({table})')}
        retired = columns & retired_columns
        if retired == retired_columns:
            legacy.add(table)
        elif retired:
            raise MigrationError('Incomplete retired prepared-input columns in ' + table)
    return dict(definitions(legacy))


def install(conn, _context):
    for sql in _admitted_definitions(conn).values():
        conn.execute(sql)


def validate(conn):
    expected = _admitted_definitions(conn)
    installed = installed_definitions(conn, expected)
    for name, sql in expected.items():
        if name not in installed or tokens(installed[name]) != tokens(sql):
            raise MigrationError('Incompatible prepared-input write relation: ' + name)
