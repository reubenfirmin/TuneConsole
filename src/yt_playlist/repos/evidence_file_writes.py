"""SQL identities and selected immutable listening evidence records."""
from yt_playlist.core.evidence_paths import record_key


_REFERENCES = """SELECT owner_id->>0,owner_id->>1,file_address,content_digest,byte_size
    FROM collection_file_writes WHERE kind='listening-evidence' AND selected=1"""


def references(conn, keys=None):
    if keys is None:
        return {(kind, key): (identity, digest, size) for kind, key, identity, digest, size in conn.execute(_REFERENCES)}
    result = {}
    for kind, key in keys:
        row = conn.execute(_REFERENCES + ' AND owner_id=json_array(?,?)', record_key(kind, key)).fetchone()
        if row:
            result[(kind, str(key))] = tuple(row[2:])
    return result


def reference_for_file(conn, identity):
    row = conn.execute(_REFERENCES + ' AND file_address=?', (identity,)).fetchone()
    return {(row[0], row[1]): tuple(row[2:])} if row else {}


def reserve(conn, records):
    result = {}
    for (kind, key), (digest, size) in records.items():
        identity = conn.execute('INSERT INTO collection_file_writes(kind,owner_id,content_digest,byte_size,file_status) '
            "VALUES ('listening-evidence',json_array(?,?),?,?,'pending')",
            (*record_key(kind, key), digest, size)).lastrowid
        result[(kind, str(key))] = identity, digest, size
    return result


def complete(conn, records, previous):
    keys = records.keys()
    if references(conn, keys) != {key: previous[key] for key in keys if key in previous}:
        raise ValueError('Listening evidence changed during file publication')
    for (kind, key), (identity, digest, size) in records.items():
        changed = conn.execute("UPDATE collection_file_writes SET file_status='ready' "
            "WHERE kind='listening-evidence' AND file_address=? "
            "AND owner_id=json_array(?,?) AND content_digest=? AND byte_size=? AND file_status='pending'",
            (identity, *record_key(kind, key), digest, size)).rowcount
        if changed != 1:
            raise ValueError('Listening evidence differs from its reservation')
        conn.execute("UPDATE collection_file_writes SET selected=0 WHERE kind='listening-evidence' "
                     'AND owner_id=json_array(?,?) AND selected=1',
                     record_key(kind, key))
        conn.execute("UPDATE collection_file_writes SET selected=1 WHERE kind='listening-evidence' AND file_address=?",
                     (identity,))
