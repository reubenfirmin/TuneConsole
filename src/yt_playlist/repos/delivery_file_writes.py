"""Committed file identities for continuing installed metadata deliveries."""
from yt_playlist.core.metadata_file_contract import record_id
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

SQL = '''CREATE TABLE delivery_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_digest TEXT NOT NULL CHECK(length(contract_digest)=64 AND contract_digest NOT GLOB '*[^0-9a-f]*'),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    delivery_id INTEGER REFERENCES metadata_deliveries(id),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)'''


def install(conn, context=None):
    actual = definitions(conn, ('delivery_file_writes',))
    if not actual:
        conn.execute(SQL)
    elif tokens(actual['delivery_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible metadata delivery file identities')


def validate(conn):
    actual = definitions(conn, ('delivery_file_writes',))
    if not actual or tokens(actual['delivery_file_writes']) != tokens(SQL):
        raise MigrationError('Incompatible metadata delivery file identities')


def reserve(conn, contract, digest, size):
    install(conn)
    return conn.execute('INSERT INTO delivery_file_writes('
        "contract_digest,content_digest,byte_size,file_status) VALUES (?,?,?,'pending')",
        (contract, digest, size)).lastrowid


def complete(conn, identity, contract, delivery, reference):
    filename, digest, size, offset = reference
    if record_id(filename) != identity or offset != 0:
        raise ValueError('Metadata delivery file differs from its reservation')
    changed = conn.execute("UPDATE delivery_file_writes SET file_status='ready',delivery_id=? WHERE id=? "
        "AND contract_digest=? AND content_digest=? AND byte_size=? AND file_status='pending' "
        'AND EXISTS (SELECT 1 FROM metadata_deliveries d WHERE d.id=? AND d.contract_digest=? '
        'AND d.raw_filename=? AND d.raw_digest=? AND d.raw_byte_size=? AND d.raw_offset=0)',
        (delivery, identity, contract, digest, size, delivery, contract, str(filename), digest, size)).rowcount
    if changed != 1:
        raise ValueError('Metadata delivery file reservation changed before completion')
