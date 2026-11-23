"""Fold singleton preview work and each evidence owner's optional generation."""
from .. import group_preview_work
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'fold_group_preview_and_evidence_storage'
RETIRED = ('group_preview_work', 'group_evidence_generations')
EVIDENCE_INDEX = 'ix_group_evidence_generation'
IMMUTABLE_OWNER = 'immutable_group_evidence_owners'
_PREVIOUS_INVALIDATION = 'UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0'


def _references(conn):
    return dict(conn.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='trigger' "
                             'AND (instr(sql,?) OR instr(sql,?)) ORDER BY name', RETIRED))


def predecessor_objects(conn):
    names = (*RETIRED, 'group_preparation_state', 'group_evidence_owners', EVIDENCE_INDEX,
             *_references(conn))
    return [sql for _, (_, sql) in sorted(objects(conn, names).items())]


def install(conn, _context):
    if conn.execute('SELECT 1 FROM group_evidence_generations GROUP BY kind,external_key '
                    'HAVING count(*)>1 LIMIT 1').fetchone():
        raise MigrationError('A musical group evidence owner has multiple generation references')

    schema_before, = conn.execute('PRAGMA main.schema_version').fetchone()
    triggers = _references(conn)
    immutable_sql = objects(conn, (IMMUTABLE_OWNER,))[IMMUTABLE_OWNER][1]
    for name in (*triggers, IMMUTABLE_OWNER):
        conn.execute(f'DROP TRIGGER {name}')

    for name, declaration in (
        ('preview_token', 'TEXT'), ('preview_schema_version', 'INTEGER'),
        ('preview_started_at', 'REAL'), ('preview_expires_at', 'REAL'),
        ('preview_invalid', 'INTEGER NOT NULL DEFAULT 0 CHECK(preview_invalid IN (0,1))'),
    ):
        conn.execute(f'ALTER TABLE group_preparation_state ADD COLUMN {name} {declaration}')
    conn.execute('''UPDATE group_preparation_state SET
        (preview_token,preview_schema_version,preview_started_at,preview_expires_at,preview_invalid)=
        (SELECT token,schema_version,started_at,expires_at,invalid FROM group_preview_work)
        WHERE singleton=1 AND EXISTS(SELECT 1 FROM group_preview_work)''')

    conn.execute('ALTER TABLE group_evidence_owners ADD COLUMN generation_id INTEGER REFERENCES group_generations(id)')
    conn.execute('''UPDATE group_evidence_owners AS owner SET generation_id=link.generation_id
        FROM group_evidence_generations link WHERE link.kind=owner.kind AND link.external_key=owner.external_key''')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    conn.execute(f'CREATE INDEX {EVIDENCE_INDEX} ON group_evidence_owners(generation_id,kind,external_key)')
    conn.execute(immutable_sql)

    for name, sql in triggers.items():
        if name == 'retained_group_publication_delete':
            conn.execute(sql.replace('FROM group_evidence_generations', 'FROM group_evidence_owners'))
        elif name == 'retired_proposal_group_references':
            start = sql.index('DELETE FROM group_evidence_generations')
            end = sql.index('; ', start) + 2
            conn.execute(sql[:start] + sql[end:])
        elif _PREVIOUS_INVALIDATION in sql:
            conn.execute(sql.replace(_PREVIOUS_INVALIDATION, group_preview_work.INVALIDATE))
        elif name not in ('immutable_group_evidence_generations',
                          'group_retention_changed_group_evidence_generations_insert',
                          'group_retention_changed_group_evidence_generations_delete'):
            raise MigrationError(f'Unmapped musical group storage dependency: {name}')
    for event, row in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
        conn.execute(f'''CREATE TRIGGER group_retention_changed_evidence_owner_{event.lower()}
            AFTER {event} ON group_evidence_owners WHEN {row}.generation_id IS NOT NULL BEGIN
            UPDATE group_preparation_state SET retention_epoch=retention_epoch+1 WHERE singleton=1; END''')

    schema_after, = conn.execute('PRAGMA main.schema_version').fetchone()
    conn.execute('UPDATE group_preparation_state SET preview_schema_version=? '
                 'WHERE singleton=1 AND preview_schema_version=? AND preview_invalid=0',
                 (schema_after, schema_before))


def validate(conn):
    installed = objects(conn, (EVIDENCE_INDEX, *RETIRED))
    if EVIDENCE_INDEX not in installed or set(installed).intersection(RETIRED):
        raise MigrationError('Musical group preview or generation evidence uses separate storage')
