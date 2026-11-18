"""SQL identities and selected immutable listening evidence records."""
from yt_playlist.core.evidence_paths import record_key
from yt_playlist.core.schema_contract import MigrationError, tokens
from yt_playlist.core.schema_inspection import definitions

TABLES = {
    'evidence_file_writes': '''CREATE TABLE evidence_file_writes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK(kind IN ('opportunity','response','forecast','artifact','report','checkpoint')),
        record_key TEXT NOT NULL,
        content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
        byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
        file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
    )''',
    'evidence_file_references': '''CREATE TABLE evidence_file_references (
        kind TEXT NOT NULL,
        record_key TEXT NOT NULL,
        file_id INTEGER NOT NULL REFERENCES evidence_file_writes(id),
        PRIMARY KEY(kind,record_key)
    ) WITHOUT ROWID''',
    'evidence_file_maintenance': '''CREATE TABLE evidence_file_maintenance (
        id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id=1),
        file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
    )''',
}


def install(conn, context=None):
    actual = definitions(conn, TABLES)
    for name, sql in TABLES.items():
        if name not in actual:
            conn.execute(sql)
        elif tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible listening evidence file identities')


def validate(conn):
    actual = definitions(conn, TABLES)
    if any(name not in actual or tokens(actual[name]) != tokens(sql) for name, sql in TABLES.items()):
        raise MigrationError('Incompatible listening evidence file identities')


def references(conn, keys=None):
    if not definitions(conn, ('evidence_file_references',)):
        return {}
    sql = "SELECT r.kind,r.record_key,w.id,w.content_digest,w.byte_size FROM evidence_file_references r " \
          "JOIN evidence_file_writes w ON w.id=r.file_id WHERE w.file_status='ready'"
    if keys is None:
        return {(kind, key): (identity, digest, size) for kind, key, identity, digest, size in conn.execute(sql)}
    result = {}
    for kind, key in keys:
        row = conn.execute(sql + ' AND r.kind=? AND r.record_key=?', record_key(kind, key)).fetchone()
        if row:
            result[(kind, str(key))] = tuple(row[2:])
    return result


def reserve(conn, records):
    install(conn)
    result = {}
    for (kind, key), (digest, size) in records.items():
        identity = conn.execute('INSERT INTO evidence_file_writes(kind,record_key,content_digest,byte_size,file_status) '
            "VALUES (?,?,?,?,'pending')", (*record_key(kind, key), digest, size)).lastrowid
        result[(kind, str(key))] = identity, digest, size
    return result


def complete(conn, records, previous):
    keys = records.keys()
    if references(conn, keys) != {key: previous[key] for key in keys if key in previous}:
        raise ValueError('Listening evidence changed during file publication')
    for (kind, key), (identity, digest, size) in records.items():
        changed = conn.execute("UPDATE evidence_file_writes SET file_status='ready' WHERE id=? AND kind=? "
            "AND record_key=? AND content_digest=? AND byte_size=? AND file_status='pending'",
            (identity, *record_key(kind, key), digest, size)).rowcount
        if changed != 1:
            raise ValueError('Listening evidence differs from its reservation')
        conn.execute('INSERT INTO evidence_file_references VALUES (?,?,?) '
                     'ON CONFLICT(kind,record_key) DO UPDATE SET file_id=excluded.file_id', (kind, str(key), identity))


def maintenance_id(conn):
    if not definitions(conn, ('evidence_file_maintenance',)):
        return None
    row = conn.execute('SELECT id FROM evidence_file_maintenance').fetchone()
    return row[0] if row else None
