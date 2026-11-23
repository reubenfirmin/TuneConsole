"""Share immutable model file reservations with other collection receipts."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from ..vibe_model_files import FILE_KIND


NAME = 'share_vibe_model_file_receipts'
RETIRED = 'vibe_model_file_writes'
MARKER = 'ix_vibe_model_file_identity'
IDENTITY = 'CAST(owner_id AS INTEGER)'
KIND = f"kind='{FILE_KIND}'"


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? '
                                        'AND sql IS NOT NULL ORDER BY type,name', (RETIRED,))]


def install_guards(conn):
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON collection_file_writes({IDENTITY}) WHERE {KIND}')
    conn.execute(f'CREATE INDEX ix_vibe_model_file_generation ON collection_file_writes('
                 f'content_digest,file_status,{IDENTITY}) WHERE {KIND}')
    for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF kind,owner_id')):
        conn.execute(f'''CREATE TRIGGER valid_vibe_model_file_identity_{suffix}
            BEFORE {event} ON collection_file_writes WHEN NEW.kind='{FILE_KIND}' AND
            (CAST(NEW.owner_id AS INTEGER)<=0 OR NEW.owner_id IS NOT CAST(CAST(NEW.owner_id AS INTEGER) AS TEXT))
            BEGIN SELECT RAISE(ABORT,'Invalid shared Vibe model file identity'); END''')


def install(conn, _context):
    conn.execute('INSERT INTO collection_file_writes(kind,owner_id,content_digest,byte_size,file_status) '
                 f'SELECT ?,id,lower(hex(generation)),byte_size,file_status FROM {RETIRED} ORDER BY id', (FILE_KIND,))
    conn.execute(f'DROP TABLE {RETIRED}')
    install_guards(conn)


def validate(conn):
    if set(definitions(conn, (MARKER, RETIRED))) != {MARKER}:
        raise MigrationError('Incompatible shared Vibe model file receipts')
