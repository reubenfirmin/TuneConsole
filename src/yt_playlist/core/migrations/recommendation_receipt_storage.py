"""Fold recommendation file reservations into the collection receipt store."""
from ..recommendation_content import FILE_IDENTITY, FILE_KIND
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence


NAME = 'share_recommendation_file_receipts'
RETIRED = 'recommendation_file_writes'
MARKER = 'ix_recommendation_file_identity'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? '
                                        'AND sql IS NOT NULL ORDER BY type,name', (RETIRED,))]


def install_guards(conn):
    conn.execute(f"CREATE UNIQUE INDEX {MARKER} ON collection_file_writes({FILE_IDENTITY}) WHERE kind='{FILE_KIND}'")
    for name, event in (('insert', 'INSERT'), ('update', 'UPDATE OF kind,owner_id,byte_size')):
        conn.execute(f'''CREATE TRIGGER valid_recommendation_file_receipt_{name}
            BEFORE {event} ON collection_file_writes WHEN NEW.kind='{FILE_KIND}' AND CASE
            WHEN NOT json_valid(NEW.owner_id) THEN 1 ELSE
                json_type(NEW.owner_id) IS NOT 'array' OR json_array_length(NEW.owner_id)<>4
                OR json_type(NEW.owner_id,'$[0]') IS NOT 'integer' OR NEW.owner_id->>0<=0
                OR json_type(NEW.owner_id,'$[1]') IS NOT 'text' OR json_type(NEW.owner_id,'$[2]') IS NOT 'text'
                OR json_type(NEW.owner_id,'$[3]') IS NOT 'text' OR NEW.byte_size>1048576 END
            BEGIN SELECT RAISE(ABORT,'Invalid recommendation file receipt'); END''')


def install(conn, _context):
    highwater = _sequence(conn, RETIRED)
    conn.execute("INSERT INTO collection_file_writes(kind,owner_id,content_digest,byte_size,file_status) "
                 f"SELECT '{FILE_KIND}',json_array(id,output_owner,surface,chunk_key),"
                 f'content_digest,byte_size,file_status FROM {RETIRED} ORDER BY id')
    _preserve_sequence(conn, 'collection_file_writes', highwater)
    conn.execute(f'DROP TABLE {RETIRED}')
    install_guards(conn)


def validate(conn):
    if set(definitions(conn, (RETIRED, MARKER))) != {MARKER}:
        raise MigrationError('Retired recommendation file receipt storage remains')
