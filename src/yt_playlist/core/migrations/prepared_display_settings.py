"""Move bounded Home, listening and report file manifests into settings."""
import json

from .. import publication_files, worker_settings
from ..recommendation_outputs import REPORT_PARTS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _sequence

NAME = 'keep_prepared_displays_in_settings'
RETIRED = tuple(publication_files.PREFIXES)
ROOTS = (
    ('group_evidence_owners', 'group_roots', 'r.value->>1=OLD.kind AND r.value->>2=OLD.external_key'),
    ('recommendation_generation_refs', 'recommendation_roots',
     'r.value->>1=OLD.kind AND r.value->>2=OLD.external_key AND r.value->>3=OLD.generation_id'),
    ('recommendation_policies', 'policy_roots', 'r.value->>1=OLD.version'),
)
GUARDS = tuple('home_retains_' + parent for parent, _, _ in ROOTS)


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        f'AND (tbl_name IN (?,?,?,?) OR name IN ({",".join("?" for _ in GUARDS)})) ORDER BY type,name',
        (*RETIRED, REPORT_PARTS, *GUARDS))]


def _copy(conn, table, prefix, fields, selected, slot):
    rows = conn.execute(f'SELECT * FROM {table} ORDER BY id')
    columns = [column[0] for column in rows.description]
    high_water = _sequence(conn, table) or 0
    for row in rows:
        manifest = dict(zip(columns, row, strict=True))
        identity = manifest['id']
        high_water = max(high_water, identity)
        key = manifest[slot] if manifest[selected] else f'retained:{identity}'
        for field in fields:
            manifest[field] = json.loads(manifest[field])
        worker_settings.save(conn, prefix + key, json.dumps(manifest, separators=(',', ':')))
    worker_settings.save(conn, publication_files.sequence_key(prefix), str(high_water))


def install(conn, _context):
    home, listening, report = RETIRED
    _copy(conn, home, publication_files.HOME, ('files', 'group_roots', 'recommendation_roots', 'policy_roots'),
          'is_current', 'surface')
    _copy(conn, listening, publication_files.LISTENING, ('files',), 'slot', 'slot')
    _copy(conn, report, publication_files.REPORT, ('parts', 'format_usage'), 'selected', 'surface')
    conn.execute('DROP VIEW ' + REPORT_PARTS)
    for guard in GUARDS:
        conn.execute('DROP TRIGGER ' + guard)
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)
    for parent, column, predicate in ROOTS:
        conn.execute(f'''CREATE TRIGGER home_retains_{parent} BEFORE DELETE ON {parent}
            WHEN EXISTS(SELECT 1 FROM settings p,json_each(p.value,'$.{column}') r
                WHERE p.key GLOB '{publication_files.HOME}*' AND {predicate})
            BEGIN SELECT RAISE(ABORT,'Home display retains this reference'); END''')


def validate(conn):
    if publication_files.installed_manifests(conn):
        return
    actual = definitions(conn, (*RETIRED, REPORT_PARTS, *GUARDS))
    if set(actual) != set(GUARDS) or any('FROM settings' not in sql for sql in actual.values()):
        raise MigrationError('Incomplete prepared display settings cutover')
