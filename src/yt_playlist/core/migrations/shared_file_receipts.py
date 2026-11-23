"""Move immutable file reservations while preserving their existing file addresses."""
from .. import collection_recovery_epoch, evidence_maintenance
from ..db_checkpoint import deferred_autocheckpoint
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence
from .runner import PreparationChanged
from .track_source_cutover import _restore_foreign_keys


NAME = 'share_metadata_evidence_and_model_file_receipts'
TABLE = 'collection_file_writes'
RETIRED = ('delivery_file_writes', 'metadata_file_writes', 'evidence_file_writes',
           'profile_file_writes', 'provenance_file_writes')
REFERENCES = {
    'recordings': ('musical_profile_file_id', 'profile_file_writes', 'profile'),
    'rec_musical_sources': ('musical_profile_file_id', 'profile_file_writes', 'profile'),
    'predictions': ('provenance_file_id', 'provenance_file_writes', 'prediction-provenance'),
}
OWNERS = (TABLE, *REFERENCES)
MARKER = 'ix_collection_file_address'
STATE_CHANGE = 'own_shared_evidence_maintenance'
PAGE_SIZE = 2048

# Preparation adds writable columns without changing the predecessor's
# constraints. Older readers continue to use the five original owners.
ADDRESS = 'previous_address INTEGER'
DELIVERY = 'delivery_id INTEGER'
SELECTED = 'selected INTEGER NOT NULL DEFAULT 0'
STAGED_COLUMNS = (
    ADDRESS,
    'file_address INTEGER AS (coalesce(previous_address,id)) VIRTUAL',
    "metadata_owner_key TEXT AS (CASE WHEN kind='runtime-metadata' THEN owner_id->>1 END) VIRTUAL",
    DELIVERY, SELECTED,
)
CONSTRAINTS = {
    ADDRESS: ' CHECK(previous_address IS NULL OR previous_address>0)',
    DELIVERY: " REFERENCES metadata_deliveries(id) CHECK(delivery_id IS NULL OR kind IS 'metadata-delivery')",
    SELECTED: " CHECK(selected IN (0,1) AND (selected=0 OR "
              "(file_status IS 'ready' AND kind IN ('runtime-metadata','listening-evidence'))))",
}
SOURCES = (
    ('delivery_file_writes', 'metadata-delivery', 'contract_digest,content_digest,0,delivery_id'),
    ('metadata_file_writes', 'runtime-metadata', 'json_array(kind,owner_key,revision_key,owner_id),content_digest,selected,NULL'),
    ('evidence_file_writes', 'listening-evidence', 'json_array(kind,record_key),content_digest,selected,NULL'),
    ('profile_file_writes', 'profile', 'kind,lower(hex(content_digest)),0,NULL'),
    ('provenance_file_writes', 'prediction-provenance', "'',lower(hex(content_digest)),0,NULL"),
)
COLUMNS = 'previous_address,byte_size,file_status,kind,owner_id,content_digest,selected,delivery_id'


def predecessor_objects(conn):
    owners = (*RETIRED, *OWNERS)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE tbl_name IN ({",".join("?" for _ in owners)}) '
        'AND sql IS NOT NULL ORDER BY type,name', owners)]


def _prepare_storage(conn):
    for declaration in STAGED_COLUMNS:
        conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN {declaration}')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON {TABLE}(kind,file_address)')
    conn.execute(f"""CREATE UNIQUE INDEX ix_collection_selected_metadata
        ON {TABLE}(metadata_owner_key,owner_id->>0,owner_id->>2)
        WHERE kind='runtime-metadata' AND selected=1""")
    conn.execute(f"""CREATE INDEX ix_collection_metadata_subject
        ON {TABLE}(owner_id->>0,owner_id->>3) WHERE kind='runtime-metadata'""")
    conn.execute(f"""CREATE UNIQUE INDEX ix_collection_selected_evidence
        ON {TABLE}(owner_id) WHERE kind='listening-evidence' AND selected=1""")
    for action, event in (('insert', 'INSERT'), ('update', 'UPDATE OF kind,owner_id,byte_size')):
        conn.execute(f"""CREATE TRIGGER collection_file_contract_{action} BEFORE {event} ON {TABLE}
            WHEN CASE NEW.kind
                WHEN 'metadata-delivery' THEN length(NEW.owner_id)<>64 OR NEW.owner_id GLOB '*[^0-9a-f]*'
                WHEN 'runtime-metadata' THEN json_type(NEW.owner_id) IS NOT 'array'
                    OR json_array_length(NEW.owner_id)<>4
                    OR coalesce(NEW.owner_id->>0,'') NOT IN ('alternatives','native','choices','conflicts')
                    OR json_type(NEW.owner_id,'$[1]') IS NOT 'text' OR length(NEW.owner_id->>1)=0
                    OR json_type(NEW.owner_id,'$[2]') IS NOT 'text' OR length(NEW.owner_id->>2)=0
                    OR json_type(NEW.owner_id,'$[3]') NOT IN ('null','integer')
                WHEN 'listening-evidence' THEN json_type(NEW.owner_id) IS NOT 'array'
                    OR json_array_length(NEW.owner_id)<>2
                    OR coalesce(NEW.owner_id->>0,'') NOT IN ('opportunity','response','forecast','artifact','report','checkpoint')
                    OR json_type(NEW.owner_id,'$[1]') IS NOT 'text'
                WHEN 'profile' THEN NEW.owner_id NOT IN ('recording','source')
                ELSE 0 END
                OR (NEW.kind IN ('metadata-delivery','runtime-metadata','listening-evidence',
                    'profile','prediction-provenance') AND NEW.byte_size>8388608)
            BEGIN SELECT RAISE(ABORT,'Invalid file receipt owner or size'); END""")

def _highwater(kind):
    return f"(SELECT coalesce(max(file_address),0) FROM {TABLE} WHERE kind='{kind}')"


def _copy(source, kind, fields):
    return (f"INSERT INTO {TABLE}({COLUMNS}) SELECT id,byte_size,file_status,'{kind}',{fields} FROM {source}")


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    if not definitions(conn, (MARKER,)):
        _prepare_storage(conn)
        return True
    for source, kind, fields in SOURCES:
        after = conn.execute('SELECT ' + _highwater(kind)).fetchone()[0]
        last = conn.execute(f'SELECT max(id) FROM (SELECT id FROM {source} WHERE id>? ORDER BY id LIMIT ?)',
                            (after, PAGE_SIZE)).fetchone()[0]
        if last is not None:
            conn.execute(f'{_copy(source, kind, fields)} WHERE id>? AND id<=? ORDER BY id', (after, last))
            return True
    return False


def prepare(conn, version):
    wal = conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    # Keep each copy page durable. Checkpoint pages together after preparation
    # instead of rewriting them while the next copy page is still pending.
    with deferred_autocheckpoint(conn, wal):
        while True:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if not _prepare_page(conn, version):
                    return


def _replace_references(conn):
    replacements = []
    for owner, (field, predecessor, kind) in REFERENCES.items():
        ddl = definitions(conn, (owner,))[owner]
        legacy = f'REFERENCES {predecessor}(id)'
        if ddl.count(legacy) != 1:
            raise MigrationError(f'Missing historical file reference on {owner}')
        ddl = ddl.replace(legacy, '').rstrip()
        ddl = ddl.replace('(', f"(file_receipt_kind TEXT AS ('{kind}') VIRTUAL,", 1)
        ddl = ddl[:-1] + (
            f',\n    FOREIGN KEY(file_receipt_kind,{field}) REFERENCES {TABLE}(kind,file_address)\n)'
        )
        replacements.append((ddl, owner))
    # Neither the new virtual columns nor the foreign keys change the
    # stored records or the keys of any existing index.
    ddl = definitions(conn, (TABLE,))[TABLE]
    for column, constraint in CONSTRAINTS.items():
        if ddl.count(column) != 1:
            raise MigrationError(f'Missing prepared file receipt column: {column}')
        ddl = ddl.replace(column, column + constraint)
    _restore_foreign_keys(conn, [*replacements, (ddl, TABLE)])


def install(conn, _context):
    if not definitions(conn, (MARKER,)) or any(conn.execute(
            f'SELECT 1 FROM {source} WHERE id>{_highwater(kind)} LIMIT 1').fetchone()
            for source, kind, _ in SOURCES):
        raise PreparationChanged()
    for source in RETIRED:
        _preserve_sequence(conn, TABLE, _sequence(conn, source))
    _replace_references(conn)
    conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN {evidence_maintenance.COLUMN} TEXT')
    conn.execute(f'''UPDATE {TABLE} SET {evidence_maintenance.COLUMN}=(
        SELECT json_object('pending',pending,'phase',phase,'checkpoint_after',checkpoint_after,
                           'windows',json(coalesce(active_windows,'{{}}')))
        FROM evidence_file_writes WHERE id=0) WHERE id=0''')
    for source in RETIRED:
        conn.execute(f'DROP TABLE {source}')


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Shared file receipt storage is incomplete')
    collection_recovery_epoch.revision(conn)


def install_state(conn, _context):
    # C182 and C183 were available with these values in settings. Restore
    # their existing state without replaying the already recorded receipt fold.
    column = evidence_maintenance.COLUMN
    if not any(row[1] == column for row in conn.execute(f'PRAGMA table_xinfo({TABLE})')):
        conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN {column} TEXT')
    conn.execute(f'''UPDATE {TABLE} SET {column}=json_patch(
        coalesce({column},'{{}}'),coalesce((SELECT value FROM settings WHERE key='listening_evidence_maintenance'),'{{}}'))
        WHERE id=0 AND EXISTS (SELECT 1 FROM settings WHERE key='listening_evidence_maintenance')''')
    conn.execute(f'''UPDATE {TABLE} SET {column}=json_set(coalesce({column},'{{}}'),'$.windows',json((
        SELECT json_group_object(substr(key,15),json(value)) FROM settings
        WHERE key IN ('choice_window:albums','choice_window:mixes','choice_window:radio'))))
        WHERE id=0 AND EXISTS (SELECT 1 FROM settings WHERE key IN ('choice_window:albums','choice_window:mixes','choice_window:radio'))''')
    conn.execute("DELETE FROM settings WHERE key IN ('listening_evidence_maintenance',"
                 "'choice_window:albums','choice_window:mixes','choice_window:radio')")


def validate_state(conn):
    collection_recovery_epoch.revision(conn)
    conn.execute(f'SELECT {evidence_maintenance.COLUMN} FROM {TABLE} WHERE id=0').fetchone()
