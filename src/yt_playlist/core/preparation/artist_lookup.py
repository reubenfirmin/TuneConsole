"""Build a reconciled normalized alias index for bounded preparation lookups."""
import hashlib
import json

from ..artist_keys import canonical_id
from ..schema_contract import MigrationError
from ...util.matching import normalize
from .rows import Job


MAX_BYTES = 64 * 1024
ALIASES = 'prepared_artist_lookup_aliases'
ORIGINALS = 'prepared_artist_lookup_originals'
MATCHES = 'prepared_artist_lookup_matches'


def copy(conn, owner, key, row):
    alias, source = bytes(row['alias']), bytes(row['source'])
    if len(alias) + len(source) > MAX_BYTES:
        raise MigrationError('Artist alias exceeds its preparation byte budget')
    alias, source = alias.decode('utf-8'), source.decode('utf-8')
    canonical = canonical_id(conn, row['artist_id'])
    snapshot = json.dumps([row['artist_id'], alias, source, canonical], ensure_ascii=False)
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?) ON CONFLICT(digest) DO NOTHING', (digest, snapshot))
    conn.execute(f'INSERT INTO {ALIASES} VALUES (?,?,?,?,?,?) ON CONFLICT(artist_id,alias,source) DO UPDATE SET '
                 'normalized_alias=excluded.normalized_alias,canonical_artist=excluded.canonical_artist,source_digest=excluded.source_digest',
                 (row['artist_id'], alias, source, normalize(alias), canonical, digest))


def delete(conn, owner, key):
    conn.execute(f'DELETE FROM {ALIASES} WHERE artist_id=? AND alias=? AND source=?', json.loads(key))


def definitions():
    yield from (
    (ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        digest TEXT PRIMARY KEY CHECK(length(digest)=64), source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot))) WITHOUT ROWID'''),
    (ALIASES, f'''CREATE TABLE {ALIASES} (
        artist_id INTEGER NOT NULL, alias TEXT NOT NULL, source TEXT NOT NULL, normalized_alias TEXT NOT NULL,
        canonical_artist INTEGER NOT NULL, source_digest TEXT NOT NULL REFERENCES {ORIGINALS}(digest),
        PRIMARY KEY(artist_id,alias,source)) WITHOUT ROWID'''),
    (MATCHES, f'''CREATE TABLE {MATCHES} (
        kind TEXT NOT NULL CHECK(kind IN ('exact','normalized')), key TEXT NOT NULL, canonical_artist INTEGER NOT NULL,
        support_count INTEGER NOT NULL CHECK(support_count>=0), PRIMARY KEY(kind,key,canonical_artist)) WITHOUT ROWID'''),
    )
    # A unique aggregate avoids scanning arbitrarily many aliases supporting
    # one canonical artist just to determine whether a second artist exists.
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        statements = []
        for kind, field in (('exact', 'alias'), ('normalized', 'normalized_alias')):
            if event != 'INSERT':
                address = f"kind='{kind}' AND key=OLD.{field} AND canonical_artist=OLD.canonical_artist"
                statements.extend((f'UPDATE {MATCHES} SET support_count=support_count-1 WHERE {address};',
                                   f'DELETE FROM {MATCHES} WHERE {address} AND support_count=0;'))
            if event != 'DELETE':
                statements.append(f"INSERT INTO {MATCHES} VALUES ('{kind}',NEW.{field},NEW.canonical_artist,1) "
                    'ON CONFLICT(kind,key,canonical_artist) DO UPDATE SET support_count=support_count+1;')
        name = 'prepared_artist_lookup_' + event.lower()
        yield name, f'CREATE TRIGGER {name} AFTER {event} ON {ALIASES} BEGIN ' + ''.join(statements) + ' END'


JOB = Job('artist_lookup', 79, 94, {'artist_aliases': ('artist_id', 'alias', 'source')}, tuple(definitions()),
    copy, delete, dependencies=('artist_redirects',), projections={'artist_aliases': ('artist_id',
    f"COALESCE(substr(CAST(alias AS BLOB),1,{MAX_BYTES + 1}),X'') AS alias",
    f"COALESCE(substr(CAST(source AS BLOB),1,{MAX_BYTES + 1}),X'') AS source")}, batch_limit=32)


def require(conn):
    progress = JOB.progress(conn)
    if progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision:
        raise MigrationError('TV artwork preparation requires a reconciled artist alias index; run the aliases job first')


def lookup(conn, value):
    if not value:
        return None
    if isinstance(value, int) or str(value).startswith('@artist:'):
        try:
            identity = int(str(value).removeprefix('@artist:'))
        except ValueError:
            return None
        return canonical_id(conn, identity) if conn.execute('SELECT 1 FROM artists WHERE id=?', (identity,)).fetchone() else None
    rows = conn.execute(f"SELECT canonical_artist FROM {MATCHES} WHERE kind='exact' AND key=? ORDER BY canonical_artist LIMIT 2", (value,)).fetchall()
    if not rows:
        rows = conn.execute(f"SELECT canonical_artist FROM {MATCHES} WHERE kind='normalized' AND key=? ORDER BY canonical_artist LIMIT 2",
                            (normalize(value),)).fetchall()
    return rows[0][0] if len(rows) == 1 else None
