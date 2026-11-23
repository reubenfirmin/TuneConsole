"""Keep immutable input digests on the group rows that use them."""
from .. import group_preview_work
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'own_group_input_digests_on_consumers'
MANIFEST = 'group_manifest_inputs'
MEMBERS = 'group_members'
RETIRED = 'group_recording_inputs'
OWNERS = (MANIFEST, MEMBERS, RETIRED)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
                                        'AND tbl_name IN (?,?,?) ORDER BY type,name', OWNERS)]


def install(conn, _context):
    tables = definitions(conn, (MANIFEST, MEMBERS))
    checks = {
        MANIFEST: ('CHECK((recording_id IS NOT NULL)<>(raw_key IS NOT NULL) AND input_digest IS NOT NULL),'),
        MEMBERS: 'CHECK((recording_id IS NOT NULL)<>(raw_key IS NOT NULL)),',
    }
    for name, sql in tables.items():
        guards = conn.execute('SELECT name,sql FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL '
                              "AND type IN ('index','trigger') ORDER BY name", (name,)).fetchall()
        sql = sql.replace('    input_id INTEGER,\n', '').replace(
            ',\n    FOREIGN KEY(input_id,recording_id) REFERENCES group_recording_inputs(id,recording_id)', '')
        begin = sql.index('CHECK((recording_id')
        end = sql.index('UNIQUE(', begin)
        sql = sql[:begin] + checks[name] + '\n    ' + sql[end:]
        _replace_table(conn, name, sql, expressions={'input_digest':
            'COALESCE(original.input_digest,(SELECT input_digest FROM group_recording_inputs WHERE id=original.input_id))'})
        for guard, statement in guards:
            if guard not in ('ix_group_manifest_input', 'ix_group_member_input') and not guard.startswith('group_preview_changed_'):
                conn.execute(statement)
    group_preview_work.install_guards(conn, sources={MEMBERS})
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    if definitions(conn, (RETIRED,)):
        raise MigrationError('Retired group input digest storage remains')
