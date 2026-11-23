"""Committed profile pack reservations; SQL only."""
from yt_playlist.repos import collection_file_writes


def reserve(conn, records):
    return [collection_file_writes.reserve(conn, 'profile', record['kind'], (record['digest'].hex(), record['size']))
        for record in records]


def complete(conn, kind, references):
    packs = {}
    for key, owner, digest, logical_size, version, body_digest, identity, size in references:
        if type(identity) is not int or identity <= 0:
            raise ValueError('Invalid profile file reservation')
        facts = digest, size
        if packs.setdefault(identity, facts) != facts:
            raise ValueError('Conflicting profile file reservation')
    for identity, (digest, size) in packs.items():
        changed = conn.execute("UPDATE collection_file_writes SET file_status='ready' "
            "WHERE kind='profile' AND file_address=? AND owner_id=? AND content_digest=? AND byte_size=? "
            "AND file_status='pending'", (identity, kind, digest.hex(), size)).rowcount
        if changed != 1:
            raise ValueError('Profile file reservation changed before completion')
