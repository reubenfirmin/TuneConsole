"""Keep imported hint state on the captured metadata observation."""
from ..metadata_hints import INDEX
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'keep_import_hints_on_metadata_observations'
RETIRED = 'metadata_hints'
VIEW = 'catalog_field_hints'
IMMUTABLE = 'immutable_metadata_observation'
RETAINED = 'retained_metadata_hint_observation'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND (tbl_name=? OR name IN (?,?,?)) ORDER BY type,name',
        (RETIRED, 'metadata_observations', VIEW, IMMUTABLE))]


def install(conn, _context):
    evidence_columns = ','.join(f'"{name}"' for name, in conn.execute(
        "SELECT name FROM pragma_table_info('metadata_observations')"))
    conn.execute("ALTER TABLE metadata_observations ADD COLUMN hint_sources TEXT NOT NULL DEFAULT '{}' "
                 "CHECK(json_type(hint_sources)='object')")
    conn.execute(f'DROP TRIGGER {IMMUTABLE}')
    conn.execute('UPDATE metadata_observations AS o SET hint_sources='
                 '(SELECT json_group_object(h.source,h.active) FROM metadata_hints h WHERE h.observation_id=o.id) '
                 'WHERE id IN (SELECT observation_id FROM metadata_hints)')
    conn.execute(f'CREATE TRIGGER {IMMUTABLE} BEFORE UPDATE OF {evidence_columns} ON metadata_observations '
                 "BEGIN SELECT RAISE(ABORT,'Metadata evidence is immutable'); END")
    conn.execute(f'CREATE TRIGGER {RETAINED} BEFORE DELETE ON metadata_observations '
                 "WHEN OLD.hint_sources<>'{}' BEGIN SELECT RAISE(ABORT,'Metadata hint retains this observation'); END")
    conn.execute(f'CREATE INDEX {INDEX} ON metadata_observations(entry_id,field) WHERE hint_sources<>\'{{}}\'')
    conn.execute(f'DROP VIEW {VIEW}')
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    names = {INDEX, IMMUTABLE, RETAINED}
    if set(definitions(conn, (*names, RETIRED, VIEW))) != names:
        raise MigrationError('Import hints were not folded into metadata observations')
