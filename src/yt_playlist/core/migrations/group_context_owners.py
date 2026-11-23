"""Keep Home group roots with their display and merge other evidence rows."""
import json

from .. import group_evidence, publication_files
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table

NAME = 'own_group_evidence_with_its_context'
RETIRED = 'group_evidence_owners'
TABLE = 'group_evidence_references'
OWNERS = (RETIRED, TABLE)
HOME_GUARD = 'home_retains_group_evidence_owners'
PROPOSAL_GUARD = 'retired_proposal_group_references'
GENERATION_GUARD = 'home_retains_group_generation'
VERSION_GUARD = 'home_retains_group_version'
DETAILS = ('reference_digest', 'created_at', 'unresolved_context_source', 'unresolved_context_reason', 'generation_id')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND (tbl_name IN (?,?) OR name=?) ORDER BY type,name', (*OWNERS, PROPOSAL_GUARD))]


def _move_home_contexts(conn):
    columns = ','.join(DETAILS)
    manifests = conn.execute(f'SELECT key,value FROM {publication_files.MANIFESTS} WHERE key GLOB ?',
                             (publication_files.HOME + '*',)).fetchall()
    moved = set()
    for key, raw in manifests:
        manifest = json.loads(raw)
        contexts = []
        for field, kind, identity in manifest['group_roots']:
            details = conn.execute(f'SELECT {columns} FROM {RETIRED} WHERE kind=? AND external_key=?',
                                   (kind, identity)).fetchone()
            if details is None:
                raise MigrationError(f'Home display {key} has no group evidence for {field}')
            references = conn.execute(f'SELECT reference_key,reference_kind,group_version_id,reason FROM {TABLE} '
                'WHERE kind=? AND external_key=? ORDER BY reference_kind,reference_key', (kind, identity))
            contexts.append((field, kind, identity, *details, [tuple(row) for row in references]))
            moved.add((kind, identity))
        if contexts:
            manifest['group_roots'] = contexts
            conn.execute(f'UPDATE {publication_files.RECEIPTS} SET display_manifest=? WHERE display_key=?',
                         (json.dumps(manifest, separators=(',', ':'), sort_keys=True), key))
    for table in (TABLE, RETIRED):
        conn.executemany(f'DELETE FROM {table} WHERE kind=? AND external_key=?', sorted(moved))


def install(conn, _context):
    guards = definitions(conn, (HOME_GUARD, PROPOSAL_GUARD, 'immutable_group_evidence_references'))
    conn.execute(f'DROP TRIGGER {HOME_GUARD}')
    conn.execute(f'DROP TRIGGER {PROPOSAL_GUARD}')
    _move_home_contexts(conn)
    _replace_table(conn, TABLE, f'''CREATE TABLE {TABLE} (
        kind TEXT NOT NULL CHECK(kind IN ('offer','response','comparison','prepared','pending','checkpoint')),
        external_key TEXT NOT NULL CHECK(length(external_key)>0),
        reference_kind TEXT NOT NULL CHECK(reference_kind IN ('context','resolved_version','version','generation')),
        reference_key TEXT NOT NULL,group_version_id INTEGER,
        reason TEXT CHECK(reason IN ('missing_generation','missing_version','wrong_generation','wrong_group')),
        reference_digest TEXT CHECK(length(reference_digest)=64 AND reference_digest NOT GLOB '*[^0-9a-f]*'),
        created_at REAL CHECK(typeof(created_at)='null' OR abs(created_at)<=1.7976931348623157e308
                              AND typeof(created_at) IN ('integer','real')),
        unresolved_context_source TEXT,unresolved_context_reason TEXT CHECK(
            (unresolved_context_source IS NULL AND unresolved_context_reason IS NULL) OR
            (unresolved_context_source IS NOT NULL AND unresolved_context_reason IS NOT NULL AND
             unresolved_context_reason IN ('missing_context','invalid_context','missing_offer','invalid_checkpoint'))),
        generation_id INTEGER REFERENCES group_generations(id),
        context_type TEXT AS (CASE WHEN reference_kind<>'context' THEN 'context' END) VIRTUAL,
        context_key TEXT AS (CASE WHEN reference_kind<>'context' THEN '' END) VIRTUAL,
        CHECK((reference_kind='context' AND reference_key='' AND reference_digest IS NOT NULL AND created_at IS NOT NULL
               AND group_version_id IS NULL AND reason IS NULL) OR
              (reference_kind<>'context' AND reference_digest IS NULL AND created_at IS NULL
               AND unresolved_context_source IS NULL AND generation_id IS NULL AND
               ((reference_kind='resolved_version' AND group_version_id IS NOT NULL AND reason IS NULL) OR
                (reference_kind<>'resolved_version' AND group_version_id IS NULL AND reason IS NOT NULL)))),
        PRIMARY KEY(kind,external_key,reference_kind,reference_key),
        FOREIGN KEY(kind,external_key,context_type,context_key)
            REFERENCES {TABLE}(kind,external_key,reference_kind,reference_key) ON DELETE CASCADE,
        FOREIGN KEY(reference_key,group_version_id) REFERENCES group_versions(external_version,id)
    ) WITHOUT ROWID''', expressions=dict.fromkeys(DETAILS, 'NULL'))
    columns = ','.join(DETAILS)
    conn.execute(f'INSERT INTO {TABLE}(kind,external_key,reference_kind,reference_key,{columns}) '
                 f"SELECT kind,external_key,'context','',{columns} FROM {RETIRED}")
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'CREATE INDEX ix_group_evidence_generation ON {TABLE}(generation_id,kind,external_key) '
                 'WHERE generation_id IS NOT NULL')
    conn.execute(f'CREATE INDEX ix_group_evidence_version ON {TABLE}(group_version_id,kind,external_key) '
                 'WHERE group_version_id IS NOT NULL')
    conn.execute(f'''CREATE VIEW {group_evidence.CONTEXTS} AS
        SELECT kind,external_key,{columns} FROM {TABLE} WHERE reference_kind='context'
        UNION ALL SELECT context.value->>1,context.value->>2,context.value->>3,context.value->>4,
            context.value->>5,context.value->>6,context.value->>7 FROM {group_evidence.HOME_CONTEXTS}''')
    conn.execute(guards['immutable_group_evidence_references'])
    proposal = guards[PROPOSAL_GUARD].split(f' DELETE FROM {RETIRED}')[0]
    conn.execute(proposal + ' END')
    conn.execute(f'''CREATE TRIGGER {GENERATION_GUARD} BEFORE DELETE ON group_generations
        WHEN EXISTS(SELECT 1 FROM {group_evidence.HOME_CONTEXTS} AND context.value->>7=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Home display retains this group generation'); END''')
    conn.execute(f'''CREATE TRIGGER {VERSION_GUARD} BEFORE DELETE ON group_versions
        WHEN EXISTS(SELECT 1 FROM {group_evidence.HOME_VERSIONS} AND reference.value->>2=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Home display retains this group version'); END''')


def validate(conn):
    names = {TABLE, group_evidence.CONTEXTS, GENERATION_GUARD, VERSION_GUARD, PROPOSAL_GUARD}
    if set(definitions(conn, (*names, RETIRED, HOME_GUARD))) != names:
        raise MigrationError('Incomplete group evidence context cutover')
