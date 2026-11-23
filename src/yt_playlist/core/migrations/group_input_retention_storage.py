"""Keep unresolved manifest inputs and retirement marks with their owners."""
from ..schema_contract import MigrationError
from ..schema_inspection import objects
from .track_source_cutover import _replace_table


NAME = 'fold_group_input_and_retirement_storage'
MANIFEST = 'group_manifest_inputs'
ALIAS_GUARD = 'immutable_group_version_aliases'
RETIRED = ('unresolved_group_manifest_inputs', 'group_retention_generations',
           'group_retention_aliases', 'group_retention_edges')
OWNERS = (MANIFEST, 'group_generations', 'group_version_aliases', 'group_lineage')
MARKER = 'uq_group_manifest_unresolved'

MANIFEST_SQL = '''CREATE TABLE group_manifest_inputs (
    manifest_id INTEGER NOT NULL REFERENCES group_input_manifests(id),
    recording_id INTEGER REFERENCES recordings(id),
    input_id INTEGER,
    raw_key TEXT,
    input_digest TEXT CHECK(input_digest IS NULL OR
        (length(input_digest)=64 AND input_digest NOT GLOB '*[^0-9a-f]*')),
    CHECK((recording_id IS NOT NULL AND input_id IS NOT NULL AND raw_key IS NULL AND input_digest IS NULL)
        OR (recording_id IS NULL AND input_id IS NULL AND raw_key IS NOT NULL AND input_digest IS NOT NULL)),
    UNIQUE(manifest_id,recording_id),
    FOREIGN KEY(input_id,recording_id) REFERENCES group_recording_inputs(id,recording_id))'''


def predecessor_objects(conn):
    tables = (*OWNERS, *RETIRED)
    placeholders = ','.join('?' for _ in tables)
    return [sql for sql, in conn.execute(f'SELECT sql FROM main.sqlite_schema WHERE tbl_name IN ({placeholders}) '
                                        'AND sql IS NOT NULL ORDER BY type,name', tables)]


def install(conn, _context):
    manifest_objects = [sql for sql, in conn.execute(
        "SELECT sql FROM main.sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') "
        'AND sql IS NOT NULL ORDER BY type,name', (MANIFEST,))]
    _replace_table(conn, MANIFEST, MANIFEST_SQL, expressions={'raw_key': 'NULL', 'input_digest': 'NULL'})
    conn.execute('INSERT INTO group_manifest_inputs(manifest_id,raw_key,input_digest) '
                 'SELECT manifest_id,raw_key,input_digest FROM unresolved_group_manifest_inputs')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON group_manifest_inputs(manifest_id,raw_key) '
                 'WHERE raw_key IS NOT NULL')
    for sql in manifest_objects:
        conn.execute(sql)

    conn.execute('DROP TRIGGER ' + ALIAS_GUARD)
    conn.execute(f'CREATE TRIGGER {ALIAS_GUARD} BEFORE UPDATE OF external_version,group_version_id,first_generation_id '
                 "ON group_version_aliases BEGIN SELECT RAISE(ABORT,'Musical group content is immutable'); END")
    for table, key, old, queue in (
        ('group_generations', 'id', 'group_retention_generations', 'generation'),
        ('group_version_aliases', 'external_version', 'group_retention_aliases', 'alias'),
    ):
        old_key = 'generation_id' if key == 'id' else key
        conn.execute(f'ALTER TABLE {table} ADD COLUMN retention_revision INTEGER')
        conn.execute(f'ALTER TABLE {table} ADD COLUMN retention_pending INTEGER NOT NULL DEFAULT 0 '
                     'CHECK(retention_pending IN (0,1))')
        conn.execute(f"ALTER TABLE {table} ADD COLUMN retention_cursor TEXT NOT NULL DEFAULT ''")
        conn.execute(f'UPDATE {table} AS owner SET retention_revision=s.walk_revision,'
                     f'retention_pending=q.pending,retention_cursor=q.cursor FROM {old} q,group_preparation_state s '
                     f"WHERE owner.{key}=q.{old_key} AND s.walk_revision=s.retention_epoch AND s.walk_phase<>'reset'")
        # Drop the old queue index before reusing its name on the owner.
        conn.execute(f'DROP TABLE {old}')
        conn.execute(f'CREATE INDEX ix_group_retention_{queue}_queue ON {table}(retention_revision,{key}) '
                     'WHERE retention_pending=1')
    conn.execute('ALTER TABLE group_lineage ADD COLUMN retention_revision INTEGER')
    conn.execute('UPDATE group_lineage AS edge SET retention_revision=s.walk_revision FROM '
                 'group_retention_edges e,group_preparation_state s WHERE edge.source_alias=e.source_alias '
                 "AND edge.target_alias=e.target_alias AND s.walk_revision=s.retention_epoch AND s.walk_phase<>'reset'")
    conn.execute("UPDATE group_preparation_state SET walk_revision=NULL WHERE walk_phase='reset'")
    conn.execute('DROP TABLE group_retention_edges')
    conn.execute('DROP TABLE unresolved_group_manifest_inputs')


def validate(conn):
    installed = objects(conn, (MARKER, *RETIRED))
    if MARKER not in installed or set(installed).intersection(RETIRED):
        raise MigrationError('Musical group inputs or retirement marks still use separate storage')
