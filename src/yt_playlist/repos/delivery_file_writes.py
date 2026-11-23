"""Committed file identities for continuing installed metadata deliveries."""
from yt_playlist.core.metadata_file_contract import record_id
from yt_playlist.repos import collection_file_writes


def reserve(conn, contract, digest, size):
    return collection_file_writes.reserve(conn, 'metadata-delivery', contract, (digest, size))


def complete(conn, identity, contract, delivery, reference):
    filename, digest, size, offset = reference
    if record_id(filename) != identity or offset != 0:
        raise ValueError('Metadata delivery file differs from its reservation')
    changed = conn.execute("UPDATE collection_file_writes SET file_status='ready',delivery_id=? "
        "WHERE kind='metadata-delivery' AND file_address=? "
        "AND owner_id=? AND content_digest=? AND byte_size=? AND file_status='pending' "
        'AND EXISTS (SELECT 1 FROM metadata_deliveries d WHERE d.id=? AND d.contract_digest=? '
        'AND d.raw_filename=? AND d.raw_digest=? AND d.raw_byte_size=? AND d.raw_offset=0)',
        (delivery, identity, contract, digest, size, delivery, contract, str(filename), digest, size)).rowcount
    if changed != 1:
        raise ValueError('Metadata delivery file reservation changed before completion')
