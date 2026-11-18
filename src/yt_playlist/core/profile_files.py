"""Bounded immutable musical-profile publications, with typed current heads (#693)."""
from yt_playlist.core import file_collection, file_paths
import hashlib
import json
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock

from . import fao, file_pack
from .evidence_ledger import canonical
from .activity import WorkBudgetExceeded
from .profile_paths import record_id, record_path
from yt_playlist.repos.profile_file_writes import head_definitions
from yt_playlist.repos.profile_file_references import KINDS, identity as _identity, install_references

MAX_ROWS = file_pack.MAX_ROWS
MAX_BYTES = file_pack.MAX_BYTES
_CHUNK_BYTES = 16 * 1024 * 1024
_CHUNKS = OrderedDict()
_CHUNK_LOCK = RLock()


class Cache(dict):
    """At most two decoded chunks and 32 MiB decompression per caller page."""
    def __init__(self, work_bytes=32 * 1024 * 1024):
        super().__init__()
        self.remaining = work_bytes


class ProfileFiles:
    def __init__(self, root):
        self.root = Path(root)

    def retained(self):
        from .policy_documents import retained_reader
        return retained_reader(self.root)

    def cursor(self, kind):
        path = self.root / ('.prepare-' + kind + '.json')
        return json.loads(fao.read(path).bytes)['after'] if path.exists() else ''

    def advance(self, kind, after):
        fao.write(self.root / ('.prepare-' + kind + '.json'), canonical({'after': after}))

    def write_legacy(self, records):
        """Explicit offline conversion; runtime writes use write_reserved."""
        references = []
        for record in records:
            fao.write(self.root / relative(record['kind'], record['digest']), record['raw'])
            references.extend((key, owner, record['digest'], size, version, digest)
                              for key, owner, size, version, digest in record['rows'])
        return references

    def originals(self, kind, references):
        cache = {}
        return [self._original(kind, reference, cache) for reference in references]

    def _original(self, kind, reference, cache):
        _, identity, publication, size, version, digest = reference[:6]
        value = _chunk_at(self.root, kind, publication, cache).get(str(identity))
        if (not isinstance(value, dict) or set(value) != {'body', 'version', 'digest'}
                or not isinstance(value['body'], str) or (value['version'], value['digest']) != (version, digest)
                or len(value['body'].encode()) != size
                or hashlib.sha256(value['body'].encode()).hexdigest() != digest):
            raise fao.CorruptFile('Musical profile differs from its exact head')
        return value['body']

    @staticmethod
    def describe(kind, values):
        """Encode bounded exact profile packs before SQL reservation."""
        if kind not in KINDS:
            raise ValueError('Invalid profile kind')
        def entries():
            for key, identity, raw, version, digest in values:
                if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != digest:
                    raise ValueError('Musical-profile body differs from its digest')
                json.loads(raw)
                record = {'body': raw, 'version': version, 'digest': digest}
                # One comma per entry, with the spare comma removed from overhead.
                size = len(canonical({str(identity): record})) - 1
                yield (key, identity, raw, version, digest), record, size

        overhead = len(canonical({'format': 'musical-profile-1', 'kind': kind, 'profiles': {}})) - 1
        packs = []
        for entries_batch in file_pack.batches(entries(), size=lambda entry: entry[2],
                max_rows=MAX_ROWS, max_bytes=MAX_BYTES, overhead=overhead):
            batch, records, originals = [], {}, []
            for original, record, _size in entries_batch:
                key, identity, raw, version, digest = original
                if str(identity) in records:
                    raise ValueError('Duplicate profile owner')
                records[str(identity)] = record
                batch.append((key, identity, len(raw.encode()), version, digest))
                originals.append(original)
            encoded = canonical({'format': 'musical-profile-1', 'kind': kind, 'profiles': records})
            file_pack.validate(encoded, len(batch), max_bytes=MAX_BYTES, max_rows=MAX_ROWS)
            publication = hashlib.sha256(encoded).digest()
            packs.append({'kind': kind, 'raw': encoded, 'digest': publication, 'size': len(encoded),
                          'values': tuple(originals), 'rows': tuple(batch)})
        return packs

    def write_reserved(self, records):
        references = []
        with self.retained(), ExitStack() as leases:
            for record in records:
                expected = self.describe(record['kind'], record['values'])
                if len(expected) != 1 or any(record[name] != expected[0][name]
                                            for name in ('raw', 'digest', 'size', 'rows')):
                    raise ValueError('Profile pack changed after reservation')
                path = self.root / record_path(record['kind'], record['file_id'])
                pending = leases.enter_context(fao.prepare(path, fao.Content(record['raw'], max_bytes=MAX_BYTES)))
                pending.publish()
                references.extend((key, owner, record['digest'], size, version, digest,
                                   record['file_id'], record['size'])
                                  for key, owner, size, version, digest in record['rows'])
        return references

    def garbage(self, before, *, limit=16):
        return _garbage(self, before, limit=limit)

    @staticmethod
    def remove(path, info):
        return fao.delete(path, expected=info.identity, missing_ok=True)

    def read_page(self, kind, rows, *, max_bytes=MAX_BYTES):
        """Decode detached exact heads, bounded by logical and chunk work bytes."""
        rows = list(rows)
        sizes = []
        for key, raw, version, digest, identity, publication, byte_size in rows:
            if not raw and publication is None:
                raise fao.CorruptFile('Required musical-profile file head is missing')
            sizes.append(len(raw.encode()) if raw else byte_size)
        if sum(sizes) > max_bytes:
            raise WorkBudgetExceeded('Musical profile page exceeds byte budget')
        cache, result = Cache(), {}
        # Corpus order interleaves profiles from many publications. Decode each
        # chunk once per page while retaining the same two-chunk/byte bounds.
        for key, raw, version, digest, identity, publication, byte_size in sorted(
                rows, key=lambda row: ((row[5][0], row[5][1]) if isinstance(row[5], tuple)
                                       else (row[5] or b'', 0))):
            if not raw:
                value = _chunk_at(self.root, kind, publication, cache).get(str(identity))
                if (not isinstance(value, dict) or set(value) != {'body', 'version', 'digest'}
                        or not isinstance(value['body'], str) or (value['version'], value['digest']) != (version, digest)
                        or len(value['body'].encode()) != byte_size
                        or hashlib.sha256(value['body'].encode()).hexdigest() != digest):
                    raise fao.CorruptFile('Musical profile differs from its exact head')
                raw = value['body']
            result[key] = json.loads(raw)
        return result


def definitions():
    yield from head_definitions()
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
    from .schema_contract import tokens as _tokens
    from .schema_inspection import definitions as read_definitions
    expected = tuple(definitions())
    actual = read_definitions(conn, (name for name, _ in expected))
    for name, sql in expected:
        if name in ('recording_profile_files', 'source_profile_files') and 'file_id' in _tokens(actual.get(name) or ''):
            from yt_playlist.repos.profile_file_writes import native_definition
            sql = native_definition(sql)
        if actual.get(name) is None or _tokens(actual[name]) != _tokens(sql):
            raise ValueError('Incompatible musical-profile file owner: ' + name)


def directory(conn):
    return file_paths.directory(conn, 'profiles')


def relative(kind, publication):
    if isinstance(publication, tuple) and len(publication) == 2:
        return record_path(kind, publication[1])
    if kind not in KINDS or not isinstance(publication, bytes) or len(publication) != 32:
        raise ValueError('Invalid profile publication identity')
    digest = publication.hex()
    return Path(kind, digest[:2], digest + '.json')


def _physical_identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _chunk(conn, kind, publication, cache):
    return _chunk_at(directory(conn), kind, publication, cache)


def _chunk_at(root, kind, publication, cache):
    identity = kind, publication
    if cache is not None and identity in cache:
        return cache[identity]
    path = root / relative(kind, publication)
    physical = _physical_identity(path)
    identity_key = (str(path), publication, physical)
    encoded = None
    if isinstance(cache, Cache):
        with _CHUNK_LOCK:
            encoded = _CHUNKS.get(identity_key)
            if encoded is not None:
                _CHUNKS.move_to_end(identity_key)
        logical = len(encoded) if encoded is not None else fao.read(path).info.logical_bytes
        if logical > cache.remaining:
            raise WorkBudgetExceeded('Musical-profile page exceeds decompression work budget')
    if encoded is None:
        encoded = fao.read(path, max_bytes=MAX_BYTES).bytes
        digest = publication[0] if isinstance(publication, tuple) else publication
        if hashlib.sha256(encoded).digest() != digest:
            raise fao.CorruptFile('Musical-profile chunk differs from its publication')
        # Retain only immutable verified bytes, never decoded mutable profiles.
        # A changed/missing file is checked again even when its SQL head stayed
        # the same. Ordinary single-record reads do not fill this page cache.
        if isinstance(cache, Cache) and _physical_identity(path) == physical:
            with _CHUNK_LOCK:
                _CHUNKS[identity_key] = encoded
                while sum(map(len, _CHUNKS.values())) > _CHUNK_BYTES or len(_CHUNKS) > 128:
                    _CHUNKS.popitem(last=False)
    if isinstance(cache, Cache):
        cache.remaining -= len(encoded)
        if cache.remaining < 0:
            raise WorkBudgetExceeded('Musical-profile page exceeds decompression work budget')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or set(value) != {'format', 'kind', 'profiles'}
            or value['format'] != 'musical-profile-1' or value['kind'] != kind
            or not isinstance(value['profiles'], dict) or not 0 < len(value['profiles']) <= MAX_ROWS):
        raise fao.CorruptFile('Invalid musical-profile chunk')
    file_pack.validate(encoded, len(value['profiles']), max_bytes=MAX_BYTES, max_rows=MAX_ROWS)
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
    from yt_playlist.repos.profile_file_writes import native
    file_id = 'file_id' if native(conn) else 'NULL'
    head = conn.execute(f'SELECT publication,byte_size,{file_id} FROM {marker} WHERE {subject}=?', (identity,)).fetchone()
    if head is None:
        raise fao.CorruptFile('Required musical-profile file head is missing')
    if head[1] > max_bytes:
        raise fao.FileBudgetExceeded('Musical profile exceeds its logical byte budget')
    publication = (head[0], head[2]) if head[2] is not None else head[0]
    value = _chunk(conn, kind, publication, cache).get(str(identity))
    if (not isinstance(value, dict) or set(value) != {'body', 'version', 'digest'}
            or not isinstance(value['body'], str) or (value['version'], value['digest']) != tuple(row[1:])
            or len(value['body'].encode()) != head[1]
            or hashlib.sha256(value['body'].encode()).hexdigest() != row[2]):
        raise fao.CorruptFile('Musical profile differs from its exact head')
    return value['body']


def read_page(conn, kind, keys, *, max_bytes=MAX_BYTES):
    """Resolve exact heads together and decode each immutable chunk once.

    Keep the same logical and decompression bounds as individual profile reads.
    Grouping by publication also avoids evicting and reopening the same chunk
    when identity-key order interleaves several publications.
    """
    return ProfileFiles(directory(conn)).read_page(kind, reference_rows(conn, kind, keys), max_bytes=max_bytes)


def reference_rows(conn, kind, keys):
    """SQL-only heads and inline predecessors, detached before file access."""
    keys = list(dict.fromkeys(keys))
    if len(keys) > 512:
        raise ValueError('Musical profile page exceeds 512 identities')
    if not keys:
        return []
    table, column, marker, subject = KINDS[kind]
    marks = ','.join('?' for _ in keys)
    if available(conn):
        from yt_playlist.repos.profile_file_writes import native
        file_id = 'h.file_id' if native(conn) else 'NULL'
        owner = 'r.id' if kind == 'recording' else 'p.source_key'
        join = 'JOIN recordings r ON r.identity_key=p.identity_key ' if kind == 'recording' else ''
        rows = conn.execute(f'SELECT p.{column},p.payload,p.version,p.digest,{owner},'
            f'h.publication,h.byte_size,{file_id} FROM {table} p {join}'
            f'LEFT JOIN {marker} h ON h.{subject}={owner} WHERE p.{column} IN ({marks}) '
            'ORDER BY h.publication', keys).fetchall()
        return [tuple(row[:5]) + ((row[5], row[7]) if row[7] is not None else row[5], row[6]) for row in rows]
    else:
        rows = conn.execute(f'SELECT {column},payload,version,digest,NULL,NULL,NULL '
            f'FROM {table} WHERE {column} IN ({marks})', keys).fetchall()
    return [tuple(row) for row in rows]


def publish(conn, kind, values):
    """Publish bodies durably, then point already-written SQL heads at them."""
    if not conn.in_transaction:
        raise RuntimeError('Profile publication requires the writer reservation')
    values = list(values)
    # Explicit offline conversion retains the installed hash format. Runtime
    # controllers use committed record IDs before touching the file port.
    references = []
    for record in ProfileFiles.describe(kind,
            [(key, _identity(conn, kind, key), raw, version, digest) for key, raw, version, digest in values]):
        fao.write(directory(conn) / relative(kind, record['digest']), record['raw'])
        references.extend((key, owner, record['digest'], size, version, digest)
                          for key, owner, size, version, digest in record['rows'])
    install_references(conn, kind, references)


def prepare_batch(conn, kind, limit=MAX_ROWS):
    from yt_playlist.controllers.profile_preparation import ProfilePreparation
    from yt_playlist.repos.profile_file_references import ProfileSources
    return ProfilePreparation(ProfileSources(conn), ProfileFiles(directory(conn))).prepare(kind, limit)


def references(conn):
    if available(conn):
        from yt_playlist.repos.profile_file_writes import native
        column = 'file_id' if native(conn) else 'NULL'
        for kind, (_, _, marker, _) in KINDS.items():
            for digest, file_id in conn.execute(f'SELECT DISTINCT publication,{column} FROM {marker}'):
                publication = (digest, file_id) if file_id is not None else digest
                yield directory(conn) / relative(kind, publication)


def recording_size_sql(alias='m'):
    return (f'CASE WHEN length(CAST({alias}.payload AS BLOB))>0 THEN length(CAST({alias}.payload AS BLOB)) '
            f'ELSE (SELECT f.byte_size FROM recording_profile_files f JOIN recordings pr ON pr.id=f.recording_id '
            f'WHERE pr.identity_key={alias}.identity_key) END')


@contextmanager
def _garbage(files, before, *, limit):
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('Profile collection budget must be 1 to 256')
    def shard(name):
        return len(name) == 2 and all(c in '0123456789abcdef' for c in name)
    # The installed cursor stores a numeric kind/shard index. The new traversal
    # covers both layouts and starts its own cursor rather than reinterpreting it.
    with file_collection.collection(files, files.root, name='.collection-v2.json', valid_shard=shard,
            prefixes=('recording', 'source', 'recording/by-id', 'source/by-id')) as work, \
            file_collection.collector(files.root) as admitted:
        if not admitted:
            yield iter(())
            return
        def candidates():
            for _ in range(limit):
                path = work.next()
                if path is None:
                    continue
                identity = record_id(path)
                parts = path.relative_to(files.root).parts
                if identity is not None:
                    reference = identity
                elif (len(parts) == 3 and path.suffix == '.json' and len(path.stem) == 64
                        and shard(path.parent.name) and path.stem[:2] == path.parent.name
                        and all(c in '0123456789abcdef' for c in path.stem)):
                    reference = bytes.fromhex(path.stem)
                else:
                    continue
                try:
                    info = fao.read(path, max_bytes=MAX_BYTES).info
                    if info.ctime_ns / 1e9 <= before:
                        yield parts[0], reference, path, info
                except (FileNotFoundError, fao.FileChanged):
                    continue
        yield candidates()
        work.save()
