"""SQL reservations for immutable, bounded prediction provenance packs."""
from yt_playlist.repos import collection_file_writes


def reserve(conn, records):
    return [collection_file_writes.reserve(conn, 'prediction-provenance', '', (record['digest'].hex(), record['size']))
            for record in records]


def complete(conn, records):
    for record in records:
        changed = conn.execute("UPDATE collection_file_writes SET file_status='ready' "
            "WHERE kind='prediction-provenance' AND file_address=? "
            "AND content_digest=? AND byte_size=? AND file_status='pending'",
            (record['file_id'], record['digest'].hex(), record['size'])).rowcount
        if changed != 1:
            raise ValueError('Prediction provenance file reservation changed before completion')
