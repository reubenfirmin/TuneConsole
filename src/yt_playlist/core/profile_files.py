"""Bounded immutable musical-profile publications, with typed current heads (#693)."""
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from . import fao
from .evidence_ledger import canonical
from .activity import WorkBudgetExceeded

MAX_ROWS = 128
MAX_BYTES = 8 * 1024 * 1024
KINDS = {'recording': ('rec_musical_profiles', 'identity_key', 'recording_profile_files', 'recording_id'),
         'source': ('rec_musical_source_profiles', 'source_key', 'source_profile_files', 'source_key')}
_COLLECTIONS = OrderedDict()
_COLLECTION_LOCK = RLock()


class _Collection:
    """One flat hash shard at a time, without buffering its directory entries."""
    def __init__(self, root, kinds):
        self.root, self.kinds = root, tuple(sorted(kinds))
        self.cursor = root / '.collection.json'
        saved = json.loads(fao.read_bytes(self.cursor)) if self.cursor.exists() else {}
        self.shard = saved.get('shard', 0)
        if type(self.shard) is not int or not 0 <= self.shard < len(self.kinds)*256:
            raise ValueError('Invalid publication collection cursor')
        self.paths = None

    def next(self):
        if self.paths is None:
            kind, shard = divmod(self.shard, 256)
            self.paths = fao.walk_entries(self.root/self.kinds[kind]/f'{shard:02x}', max_depth=0)
        try:
            return next(self.paths)
        except StopIteration:
            self.paths.close()
            self.paths = None
            self.shard = (self.shard+1) % (len(self.kinds)*256)
            return None

    def close(self):
        if self.paths is not None:
            self.paths.close()
            self.paths = None


def _collection(conn, root, kinds):
    # The application connection closes its iterators at shutdown. Also bound
    # standalone SQLite callers: eviction/restart safely repeats one hash shard.
    key = conn, root, tuple(sorted(kinds))
    work = _COLLECTIONS.pop(key, None)
    if work is None:
        work = _Collection(root, kinds)
        if len(_COLLECTIONS) == 8:
            _COLLECTIONS.popitem(last=False)[1].close()
        if hasattr(conn, '_close_callbacks'):
            def release():
                with _COLLECTION_LOCK:
                    if _COLLECTIONS.get(key) is work:
                        del _COLLECTIONS[key]
                    work.close()
            conn._close_callbacks.append(release)
    _COLLECTIONS[key] = work
    return work


class Cache(dict):
    """At most two decoded chunks and 32 MiB decompression per caller page."""
    def __init__(self, work_bytes=32 * 1024 * 1024):
        super().__init__()
        self.remaining = work_bytes


def definitions():
    yield ('recording_profile_files', 'CREATE TABLE recording_profile_files ('
        'recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,'
        'publication BLOB NOT NULL CHECK(length(publication)=32),byte_size INTEGER NOT NULL CHECK(byte_size>0))')
    yield ('source_profile_files', 'CREATE TABLE source_profile_files ('
        'source_key TEXT PRIMARY KEY NOT NULL REFERENCES rec_musical_source_profiles(source_key) ON DELETE CASCADE,'
        'publication BLOB NOT NULL CHECK(length(publication)=32),byte_size INTEGER NOT NULL CHECK(byte_size>0)) WITHOUT ROWID')
    yield ('recording_profile_file_delete', 'CREATE TRIGGER recording_profile_file_delete AFTER DELETE ON rec_musical_profiles '
        'BEGIN DELETE FROM recording_profile_files WHERE recording_id=(SELECT id FROM recordings WHERE identity_key=OLD.identity_key); END')
    for kind, (table, key, marker, column) in KINDS.items():
        subject = '(SELECT id FROM recordings WHERE identity_key=OLD.identity_key)' if kind == 'recording' else 'OLD.source_key'
        name = kind + '_profile_plain_update'
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE OF payload ON {table} WHEN NEW.payload<>\'\' '
                     f'BEGIN DELETE FROM {marker} WHERE {column}={subject}; END')


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recording_profile_files'").fetchone() is not None


def install(conn, context=None):
    for name, sql in definitions():
        conn.execute(sql)


def validate(conn):
    from .migrations.current import _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise ValueError('Incompatible musical-profile file owner: ' + name)


def directory(conn):
    return fao.directory(conn, 'profiles')


def relative(kind, publication):
    if kind not in KINDS or not isinstance(publication, bytes) or len(publication) != 32:
        raise ValueError('Invalid profile publication identity')
    digest = publication.hex()
    return Path(kind, digest[:2], digest + '.json')


def _identity(conn, kind, key):
    if kind == 'source':
        return key
    row = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    if row is None:
        raise ValueError('Unknown recording profile owner')
    return row[0]


def _chunk(conn, kind, publication, cache):
    identity = kind, publication
    if cache is not None and identity in cache:
        return cache[identity]
    path = directory(conn) / relative(kind, publication)
    if isinstance(cache, Cache) and fao.stat(path).logical_bytes > cache.remaining:
        raise WorkBudgetExceeded('Musical-profile page exceeds decompression work budget')
    encoded = fao.read_bytes(path, max_bytes=MAX_BYTES)
    if isinstance(cache, Cache):
        cache.remaining -= len(encoded)
        if cache.remaining < 0:
            raise WorkBudgetExceeded('Musical-profile page exceeds decompression work budget')
    if hashlib.sha256(encoded).digest() != publication:
        raise fao.CorruptFile('Musical-profile chunk differs from its publication')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or set(value) != {'format', 'kind', 'profiles'}
            or value['format'] != 'musical-profile-1' or value['kind'] != kind
            or not isinstance(value['profiles'], dict) or not 0 < len(value['profiles']) <= MAX_ROWS):
        raise fao.CorruptFile('Invalid musical-profile chunk')
    if cache is not None:
        while len(cache) >= 2:
            del cache[next(iter(cache))]
        cache[identity] = value['profiles']
    return value['profiles']


def size(conn, kind, key):
    table, column, marker, subject = KINDS[kind]
    row = conn.execute(f'SELECT length(CAST(payload AS BLOB)) FROM {table} WHERE {column}=?', (key,)).fetchone()
    if row is None:
        return 0
    if row[0] or not available(conn):
        return row[0]
    head = conn.execute(f'SELECT byte_size FROM {marker} WHERE {subject}=?', (_identity(conn, kind, key),)).fetchone()
    if head is None:
        raise fao.CorruptFile('Required musical-profile file head is missing')
    return head[0]


def read(conn, kind, key, *, cache=None, max_bytes=MAX_BYTES):
    table, column, marker, subject = KINDS[kind]
    row = conn.execute(f'SELECT payload,version,digest FROM {table} WHERE {column}=?', (key,)).fetchone()
    if row is None:
        return None
    if row[0]:
        if len(row[0].encode()) > max_bytes:
            raise fao.FileBudgetExceeded('Musical profile exceeds its logical byte budget')
        return row[0]
    identity = _identity(conn, kind, key)
    head = conn.execute(f'SELECT publication,byte_size FROM {marker} WHERE {subject}=?', (identity,)).fetchone()
    if head is None:
        raise fao.CorruptFile('Required musical-profile file head is missing')
    if head[1] > max_bytes:
        raise fao.FileBudgetExceeded('Musical profile exceeds its logical byte budget')
    value = _chunk(conn, kind, head[0], cache).get(str(identity))
    if (not isinstance(value, dict) or set(value) != {'body', 'version', 'digest'}
            or not isinstance(value['body'], str) or (value['version'], value['digest']) != tuple(row[1:])
            or len(value['body'].encode()) != head[1]
            or hashlib.sha256(value['body'].encode()).hexdigest() != row[2]):
        raise fao.CorruptFile('Musical profile differs from its exact head')
    return value['body']


def publish(conn, kind, values):
    """Publish bodies durably, then point already-written SQL heads at them."""
    if not conn.in_transaction:
        raise RuntimeError('Profile publication requires the writer reservation')
    values = list(values)
    table, key_column, marker, subject = KINDS[kind]
    offset = 0
    while offset < len(values):
        batch, records, used = [], {}, 256
        for key, raw, version, digest in values[offset:offset+MAX_ROWS]:
            identity = _identity(conn, kind, key)
            if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != digest:
                raise ValueError('Musical-profile body differs from its digest')
            json.loads(raw)
            head = conn.execute(f'SELECT version,digest FROM {table} WHERE {key_column}=?', (key,)).fetchone()
            if head is None or tuple(head) != (version, digest):
                raise ValueError('Stale musical-profile publication')
            record = {'body': raw, 'version': version, 'digest': digest}
            size = len(canonical({str(identity):record}))+1
            if used+size > MAX_BYTES:
                if not batch:
                    raise fao.FileBudgetExceeded('Musical profile exceeds the chunk byte budget')
                break
            records[str(identity)] = record
            batch.append((key,raw,version,digest))
            used += size
        encoded = canonical({'format': 'musical-profile-1', 'kind': kind, 'profiles': records})
        publication = hashlib.sha256(encoded).digest()
        fao.write_bytes(directory(conn) / relative(kind, publication), encoded, immutable=True, max_bytes=MAX_BYTES)
        for key, raw, version, digest in batch:
            conn.execute(f'INSERT INTO {marker} VALUES (?,?,?) ON CONFLICT({subject}) DO UPDATE '
                'SET publication=excluded.publication,byte_size=excluded.byte_size',
                (_identity(conn, kind, key), publication, len(raw.encode())))
            conn.execute(f"UPDATE {table} SET payload='' WHERE {key_column}=?", (key,))
        offset += len(batch)


def prepare_batch(conn, kind, limit=MAX_ROWS):
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError('Profile preparation batch must be 1 to 128')
    table, key, marker, subject = KINDS[kind]
    # A file cursor avoids rescanning the converted prefix. It is only a hint:
    # legacy writers behind it are picked up on the next complete pass.
    cursor = directory(conn) / ('.prepare-' + kind + '.json')
    after = json.loads(fao.read_bytes(cursor))['after'] if cursor.exists() else ''
    rows = conn.execute(f"SELECT {key},payload,version,digest FROM {table} WHERE {key}>? "
        f"AND payload<>'' ORDER BY {key} LIMIT ?", (after, limit)).fetchall()
    publish(conn, kind, rows)
    cache = {}
    for key, raw, version, digest in rows:
        if read(conn, kind, key, cache=cache) != raw:
            raise fao.CorruptFile('Profile preparation changed original bytes')
    # Advancing before commit is safe: a rollback leaves nonempty bodies behind
    # the hint and the next wrap will retry them. Completion always checks them.
    fao.write_bytes(cursor, canonical({'after': rows[-1][0] if len(rows) == limit else ''}))
    if not rows and after:
        return prepare_batch(conn, kind, limit)
    return len(rows)


def references(conn):
    if available(conn):
        for kind, (_, _, marker, _) in KINDS.items():
            for (publication,) in conn.execute(f'SELECT DISTINCT publication FROM {marker}'):
                yield directory(conn) / relative(kind, publication)


def recording_size_sql(alias='m'):
    return (f'CASE WHEN length(CAST({alias}.payload AS BLOB))>0 THEN length(CAST({alias}.payload AS BLOB)) '
            f'ELSE (SELECT f.byte_size FROM recording_profile_files f JOIN recordings pr ON pr.id=f.recording_id '
            f'WHERE pr.identity_key={alias}.identity_key) END')


def export(conn, target):
    return fao.export(directory(conn), references(conn), target)


def restore(conn, manifest):
    fao.restore(directory(conn), manifest, expected=references(conn))


def collect(conn, now, limit=16):
    """Bounded round-robin orphan maintenance; pinned heads never expire."""
    return collect_unreferenced(conn, directory(conn),
        {kind: values[2] for kind, values in KINDS.items()}, now, limit, '*/*/*.json')


def collect_unreferenced(conn, root, markers, now, limit, pattern):
    """Shared immutable-publication collector; owner tables remain explicit."""
    if conn.in_transaction:
        raise RuntimeError('Profile collection requires committed publication')
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('Publication collection budget must be 1 to 256')
    root = Path(root)
    with _COLLECTION_LOCK, fao.collector(root) as admitted:
        if not admitted:
            return 0
        # Serialize the owner recheck and deletion with every SQL publisher.
        conn.execute('BEGIN IMMEDIATE')
        try:
            work = _collection(conn, root, markers)
            removed = 0
            for _ in range(limit):
                path = work.next()
                if path is None or not path.relative_to(root).match(pattern):
                    continue
                kind = path.relative_to(root).parts[0]
                if len(path.stem) != 64 or any(char not in '0123456789abcdef' for char in path.stem):
                    continue
                publication = bytes.fromhex(path.stem)
                marker = markers[kind]
                try:
                    info = fao.stat(path)
                    if (not conn.execute(f'SELECT 1 FROM {marker} WHERE publication=? LIMIT 1', (publication,)).fetchone()
                            and fao.Retention(30, 'ctime').eligible(info, now)):
                        removed += fao.delete(path, expected=info.identity, missing_ok=True)
                except (FileNotFoundError, fao.FileChanged):
                    continue
            fao.write_bytes(work.cursor, canonical({'shard': work.shard}))
            return removed
        finally:
            conn.rollback()
