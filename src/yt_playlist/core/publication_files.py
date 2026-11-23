"""Exact file coordinates retained on Home and listening publications."""
import json

from . import worker_settings
from .db_revisions import keyed_reads
from .recommendation_content import FILE_IDENTITY, FILE_KIND, record_id

OWNERS = {'home_display_files': 'home_display_publications',
          'listening_output_files': 'listening_output_publications'}
HOME = 'prepared_home:'
LISTENING = 'prepared_listening:'
REPORT = 'prepared_report:'
REPORT_TABLE = 'recommendation_report_publications'
PREFIXES = {'home_display_publications': HOME, 'listening_output_publications': LISTENING,
            REPORT_TABLE: REPORT}
INDEX = 'ix_prepared_display_key'
RECEIPTS = 'collection_file_writes'
MANIFESTS = 'prepared_display_manifests'
EMPTY_LISTENING = 'empty_listening_display:'


def sequence_key(prefix):
    return prefix.rstrip(':') + '_id_high_water'


def next_identity(conn, prefix):
    key = sequence_key(prefix)
    identity = int(worker_settings.read(conn, key, '0')) + 1
    worker_settings.save(conn, key, str(identity))
    return identity


def _empty_key(key):
    return EMPTY_LISTENING + key.removeprefix(LISTENING)


def clear(conn, key):
    conn.execute(f"DELETE FROM {RECEIPTS} WHERE kind='prepared-display' AND display_key=?", (key,))
    conn.execute(f'UPDATE {RECEIPTS} SET display_key=NULL,display_manifest=NULL WHERE display_key=?', (key,))
    if key.startswith(LISTENING):
        worker_settings.save(conn, _empty_key(key), None)


def move_listening(conn, source, destination):
    clear(conn, destination)
    conn.execute(f'UPDATE {RECEIPTS} SET display_key=? WHERE display_key=?', (destination, source))
    conn.execute('UPDATE settings SET key=? WHERE key=?', (_empty_key(destination), _empty_key(source)))


def save(conn, key, value):
    clear(conn, key)
    prefix = next(prefix for prefix in PREFIXES.values() if key.startswith(prefix))
    raw = json.dumps(value, separators=(',', ':'))
    if prefix == REPORT:
        parts = [(value['surface'], str(position), *part[1:])
                 for position, part in enumerate(value['parts']) if part is not None]
        owner = 'recommendation_report_files'
    else:
        parts = [part[1:6] for part in value['files']]
        owner = 'home_display_files' if prefix == HOME else 'listening_output_files'
    for surface, chunk, filename, digest, size in parts:
        identity = record_id(filename)
        if identity is not None and conn.execute(f'''UPDATE {RECEIPTS} SET display_key=?,display_manifest=?
            WHERE kind='{FILE_KIND}' AND {FILE_IDENTITY}=? AND display_key IS NULL AND file_status='ready'
                AND owner_id->>1=? AND owner_id->>2=? AND owner_id->>3=? AND content_digest=? AND byte_size=?''',
                (key, raw, identity, owner, surface, chunk, digest, size)).rowcount:
            return
    if parts:
        # Content-addressed parts have a real file but no numeric write receipt.
        # Admit their first part without copying, renaming or reopening its bytes.
        _, _, filename, digest, size = parts[0]
        conn.execute(f"INSERT INTO {RECEIPTS}(kind,owner_id,content_digest,byte_size,file_status,display_key,display_manifest) "
                     "VALUES ('prepared-display',?,?,?,'ready',?,?)", (filename, digest, size, key, raw))
    else:
        if prefix != LISTENING:
            raise ValueError('Prepared display requires at least one file')
        worker_settings.save(conn, _empty_key(key), raw)


def fields(conn, key, *fields):
    columns = ','.join("value->>'" + field + "'" for field in fields)
    with keyed_reads(conn, RECEIPTS, (key,)):
        return conn.execute(f'SELECT {columns} FROM {MANIFESTS} WHERE key=?', (key,)).fetchone()


def key(owner):
    return owner + '_id_high_water'


def manifest(conn, owner, descriptors):
    count = len(descriptors)
    if not count:
        return '[]'
    end = conn.execute('UPDATE settings SET value=CAST(value AS INTEGER)+? WHERE key=? '
                       'RETURNING CAST(value AS INTEGER)', (count, key(owner))).fetchone()[0]
    return json.dumps([(identity, *descriptor)
                       for identity, descriptor in enumerate(descriptors, end - count + 1)])


def references(conn, table):
    if not installed_table(conn, table):
        relation = MANIFESTS if installed_manifests(conn) else 'settings'
        return manifest_references(conn, PREFIXES[table], relation=relation)
    return conn.execute(f'''SELECT f.value->>0,f.value->>1,f.value->>2,f.value->>3,f.value->>4,f.value->>5
        FROM {table} p,json_each(p.files) f ORDER BY f.value->>0''')


def installed_table(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone() is not None


def installed_manifests(conn):
    return conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (INDEX,)).fetchone() is not None


def manifest_references(conn, prefix, *, relation=MANIFESTS):
    if prefix == REPORT:
        keys = "f.value->>0,p.value->>'surface',CAST(f.key AS TEXT),f.value->>1,f.value->>2,f.value->>3"
        field = 'parts'
    else:
        keys = 'f.value->>0,f.value->>1,f.value->>2,f.value->>3,f.value->>4,f.value->>5'
        field = 'files'
    return conn.execute(f"SELECT {keys} FROM {relation} p,json_each(p.value,'$.{field}') f "
                        "WHERE p.key GLOB ? AND f.type='array' ORDER BY f.value->>0", (prefix + '*',))


def protected(conn, table, names):
    if not installed_table(conn, table):
        relation = MANIFESTS if installed_manifests(conn) else 'settings'
        return protected_parts(conn, PREFIXES[table], names, relation=relation)
    markers = ','.join('?' for _ in names)
    return {name for name, in conn.execute(
        f'SELECT DISTINCT f.value->>3 FROM {table} p,json_each(p.files) f '
        f'WHERE f.value->>3 IN ({markers})', names)}


def protected_parts(conn, prefix, names, *, relation=MANIFESTS):
    field, position = ('parts', 1) if prefix == REPORT else ('files', 3)
    return {name for name, in conn.execute(
        f"SELECT f.value->>{position} FROM {relation} p,json_each(p.value,'$.{field}') f "
        f"WHERE p.key GLOB ? AND f.type='array' AND f.value->>{position} IN ({','.join('?' for _ in names)})",
        (prefix + '*', *names))}


def relocate(conn, table, replacements):
    if not installed_table(conn, table):
        prefix = PREFIXES[table]
        field, position = ('parts', 1) if prefix == REPORT else ('files', 3)
        current = installed_manifests(conn)
        relation = MANIFESTS if current else 'settings'
        for key, raw in conn.execute(f'SELECT key,value FROM {relation} WHERE key GLOB ?', (prefix + '*',)).fetchall():
            manifest = json.loads(raw)
            if _replace_filenames(manifest[field], replacements, position):
                encoded = json.dumps(manifest, separators=(',', ':'))
                if current:
                    conn.execute(f'UPDATE {RECEIPTS} SET display_manifest=? WHERE display_key=?', (encoded, key))
                else:
                    worker_settings.save(conn, key, encoded)
        return
    if table == REPORT_TABLE:
        for identity, filename in replacements.items():
            conn.execute('UPDATE recommendation_report_parts SET filename=? WHERE id=?', (filename, identity))
        return
    for identity, encoded in conn.execute(f'SELECT id,files FROM {table}').fetchall():
        rows = json.loads(encoded)
        if _replace_filenames(rows, replacements, 3):
            conn.execute(f'UPDATE {table} SET files=? WHERE id=?', (json.dumps(rows), identity))


def _replace_filenames(rows, replacements, position):
    changed = False
    for row in rows:
        if row is not None and row[0] in replacements and row[position] != replacements[row[0]]:
            row[position] = replacements[row[0]]
            changed = True
    return changed
