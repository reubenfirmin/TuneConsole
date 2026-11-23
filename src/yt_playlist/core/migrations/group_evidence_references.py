"""Keep the outcome of every exact group evidence reference in one owner."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'own_resolved_and_unresolved_group_evidence'
TABLE = 'group_evidence_references'
PREVIOUS = 'group_evidence_versions'
RETIRED = 'unresolved_group_evidence'
OWNERS = (PREVIOUS, RETIRED, TABLE)
GUARDS = ('retained_group_publication_delete', 'retired_proposal_group_references')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND (tbl_name IN (?,?) OR name IN (?,?)) ORDER BY type,name', (PREVIOUS, RETIRED, *GUARDS))]


def install(conn, _context):
    guards = definitions(conn, GUARDS)
    for name in GUARDS:
        conn.execute(f'DROP TRIGGER {name}')
    table = f'''CREATE TABLE {PREVIOUS} (
        kind TEXT NOT NULL,external_key TEXT NOT NULL,reference_key TEXT NOT NULL,group_version_id INTEGER,
        reference_kind TEXT NOT NULL CHECK(reference_kind IN ('resolved_version','version','generation')),
        reason TEXT CHECK(reason IN ('missing_generation','missing_version','wrong_generation','wrong_group')),
        CHECK((reference_kind='resolved_version' AND group_version_id IS NOT NULL AND reason IS NULL)
           OR (reference_kind<>'resolved_version' AND group_version_id IS NULL AND reason IS NOT NULL)),
        PRIMARY KEY(kind,external_key,reference_kind,reference_key),
        FOREIGN KEY(kind,external_key) REFERENCES group_evidence_owners(kind,external_key),
        FOREIGN KEY(reference_key,group_version_id) REFERENCES group_version_aliases(external_version,group_version_id)
    ) WITHOUT ROWID'''
    _replace_table(conn, PREVIOUS, table, expressions={
        'reference_key': 'external_version', 'reference_kind': "'resolved_version'", 'reason': 'NULL'})
    conn.execute(f'ALTER TABLE {PREVIOUS} RENAME TO {TABLE}')
    conn.execute(f'INSERT INTO {TABLE}(kind,external_key,reference_kind,reference_key,reason) '
                 f'SELECT kind,external_key,reference_kind,raw_key,reason FROM {RETIRED}')
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'CREATE INDEX ix_group_evidence_version ON {TABLE}(group_version_id,kind,external_key)')
    conn.execute(f'CREATE INDEX ix_group_evidence_walk ON {TABLE}(kind,external_key,reference_key) '
                 "WHERE reference_kind='resolved_version'")
    conn.execute(f'''CREATE TRIGGER immutable_group_evidence_references BEFORE UPDATE ON {TABLE}
        BEGIN SELECT RAISE(ABORT,'Musical group evidence references are immutable'); END''')
    for event, row in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
        conn.execute(f'''CREATE TRIGGER group_retention_changed_evidence_reference_{event.lower()}
            AFTER {event} ON {TABLE} WHEN {row}.group_version_id IS NOT NULL BEGIN
            UPDATE group_preparation_state SET retention_epoch=retention_epoch+1 WHERE singleton=1; END''')
    conn.execute(guards[GUARDS[0]].replace(PREVIOUS, TABLE).replace('e.external_version', 'e.reference_key'))
    proposal = guards[GUARDS[1]].replace(PREVIOUS, TABLE)
    start = proposal.index(f'DELETE FROM {RETIRED}')
    end = proposal.index('; ', start) + 2
    conn.execute(proposal[:start] + proposal[end:])


def validate(conn):
    if set(definitions(conn, OWNERS)) != {TABLE}:
        raise MigrationError('Retired group evidence storage remains')
