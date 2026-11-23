"""Keep the selected recording and artist embeddings on their existing owners."""
from .. import embedding_files, file_receipts
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged
from .track_source_cutover import _replace_table


NAME = 'fold_embedding_storage'
MARKER = 'ix_recording_embedding_files_file_id'
HEADS = ('recording_embedding_files', 'artist_embedding_files')
INLINE = 'recording_embeddings'
RECEIPTS = 'embedding_file_writes'
RETIRED = (INLINE, RECEIPTS)
OBSOLETE_TRIGGERS = (
    'recording_embedding_files_file_basis_insert', 'recording_embedding_files_file_basis_update',
    'retire_recording_embeddings_insert', 'retire_recording_embeddings_update',
)
LOCATOR = "file_id INTEGER CHECK(file_id IS NULL OR (typeof(file_id)='integer' AND file_id>0))"


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        '(tbl_name IN (?,?,?,?) OR name IN (?,?,?,?)) ORDER BY type,name',
        (*HEADS, *RETIRED, *OBSOLETE_TRIGGERS))]


def _has_inline(conn):
    return conn.execute(f'SELECT 1 FROM {INLINE} LIMIT 1').fetchone() is not None


def prepare(conn, version):
    # Use the same bounded converter as the earlier artist embedding cutover.
    while True:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            if (conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone()
                    or not _has_inline(conn)):
                return
            embedding_files.prepare_batch(conn, 'recording')


def install(conn, _context):
    if _has_inline(conn):
        raise PreparationChanged()
    for table in HEADS:
        kind = table.split('_', 1)[0]
        if conn.execute(f'''
            SELECT 1 FROM {table} h LEFT JOIN {RECEIPTS} f ON f.id=h.file_id
            WHERE h.file_id IS NOT NULL AND (f.file_status IS NOT 'ready' OR f.kind IS NOT ?
                OR f.dimensions IS NOT h.dimensions OR f.content_digest IS NOT h.publication
                OR f.byte_size < ? + (h.position+1)*h.dimensions*4)
            LIMIT 1
        ''', (kind, embedding_files.HEADER.size)).fetchone():
            raise MigrationError('An embedding file locator does not match its selected data')
    for name in OBSOLETE_TRIGGERS:
        conn.execute(f'DROP TRIGGER {name}')
    for table in HEADS:
        sql = definitions(conn, (table,))[table]
        owned_objects = [ddl for ddl, in conn.execute(
            'SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN (\'index\',\'trigger\') '
            'AND sql IS NOT NULL ORDER BY type,name', (table,))]
        _replace_table(conn, table, sql.replace(file_receipts.EMBEDDING_FILE_COLUMN, LOCATOR))
        for ddl in owned_objects:
            conn.execute(ddl)
        conn.execute(f'CREATE INDEX ix_{table}_file_id ON {table}(file_id) WHERE file_id IS NOT NULL')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Embedding storage was not consolidated')
