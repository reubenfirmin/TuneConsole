"""Store policy references directly on their On Air publication."""
from ..radio_policy_roots import SESSION_VERSIONS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_radio_publication_policies'
OWNERS = ('radio_sessions', 'radio_seed_output')
VERSIONS = SESSION_VERSIONS + ' UNION SELECT r.key FROM radio_seed_output o,json_each(o.policy_roots) r WHERE r.value=1'
RETIRED = ('radio_session_policies', 'radio_seed_policies')
KEYS = ('session_id', 'offer_id')
COLUMN = "policy_roots TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(policy_roots) AND json_type(policy_roots)='object')"
MARKER = 'retained_radio_policy_delete'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM main.sqlite_schema WHERE tbl_name IN (?,?,?,?) AND sql IS NOT NULL ORDER BY type,name',
        (*OWNERS, *RETIRED))]


def install(conn, _context):
    for table, previous, key in zip(OWNERS, RETIRED, KEYS, strict=True):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {COLUMN}')
        conn.execute(f'UPDATE {table} AS owner SET policy_roots=(SELECT '
                     f'json_group_object(raw_version,policy_version IS NOT NULL ORDER BY raw_version) '
                     f'FROM {previous} WHERE {key}=owner.id) '
                     f'WHERE EXISTS(SELECT 1 FROM {previous} WHERE {key}=owner.id)')
        conn.execute(f'DROP TABLE {previous}')
        for action, event in (('insert', 'INSERT'), ('update', 'UPDATE OF policy_roots')):
            conn.execute(f'''CREATE TRIGGER complete_{table}_policy_{action} BEFORE {event} ON {table}
                WHEN EXISTS(SELECT 1 FROM json_each(NEW.policy_roots) r WHERE
                    r.type<>'integer' OR r.value NOT IN (0,1) OR (r.value=1 AND NOT EXISTS(
                        SELECT 1 FROM recommendation_policies p WHERE p.version=r.key)))
                    OR EXISTS(SELECT 1 FROM json_each(NEW.policy_roots) GROUP BY key HAVING count(*)>1)
                BEGIN SELECT RAISE(ABORT,'Invalid On Air policy references'); END''')
    for action, event in (('delete', 'DELETE'), ('update', 'UPDATE OF version')):
        changed = 'NEW.version IS NOT OLD.version AND ' if action == 'update' else ''
        conn.execute(f'''CREATE TRIGGER retained_radio_policy_{action} BEFORE {event} ON recommendation_policies
            WHEN {changed}OLD.version IN ({VERSIONS})
            BEGIN SELECT RAISE(ABORT,'On Air still references this policy'); END''')


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Incompatible On Air policy storage')
