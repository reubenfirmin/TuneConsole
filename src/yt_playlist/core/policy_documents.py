"""Policy-owned immutable configurations, packed reads and SQL-view leases."""
import hashlib
import json
import os
import math
from contextlib import contextmanager
from itertools import chain, groupby
from pathlib import Path
import tempfile

from .evidence_ledger import canonical
from .file_collection import retained_reader
from .policy_config import BASE_FIELD
from . import fao
from .fao import _try_file_lock

FORMAT = 'recommendation-policy-1'
MAX_BYTES = 8 * 1024 * 1024
PACK_BYTES = 256 * 1024
PACK_HEADER = b'TuneConsole recommendation policies 1\n'
OWNERS = ('recommendation_policy_bodies', 'recommendation_policies')


def root_for_database(path, memory_key):
    if path is not None:
        database = Path(path).resolve()
        return database.with_name(database.name + '.policies')
    return Path(tempfile.gettempdir()) / 'tuneconsole-policies' / memory_key


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        return root_for_database(path, memory_key)
    if memory_key is None:
        memory_key = getattr(conn, '_policy_memory_key', f'{os.getpid()}-{id(conn)}')
    return root_for_database(None, memory_key)


def publish(root, configuration):
    if not isinstance(configuration, dict):
        raise ValueError('Recommendation configuration must be an object')
    encoded = canonical({'format': FORMAT, 'configuration': configuration})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Recommendation policy exceeds its file byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    return _publish_encoded(root, encoded, digest), digest, len(encoded)


def _publish_encoded(root, encoded, digest, *, age=None):
    path = Path(root) / digest[:2] / (digest + '.json')
    fao.write(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES))
    return str(path)


def _existing(path, size=MAX_BYTES):
    with fao.read(path) as stream:
        return stream.read(size + 1)


def pack_header(header, age):
    """Keep custody clocks in a recovered pack's content-addressed identity.

    Equal retained records can come from different, differently aged packs.
    Their bytes alone cannot identify both publications. A bounded header suffix
    distinguishes those clocks without changing any owned record or keeping a
    separate file registry. Installed headers and record ranges remain readable.
    """
    if age is None:
        return header
    return header + b'Custody clocks 1 ' + canonical([age.mtime_ns, age.ctime_ns]) + b'\n'


def pack_encoded(root, records, *, header, pack_bytes, age=None):
    """Publish validated records lazily, admitting one record above the pack target."""
    header = pack_header(header, age)
    pending, size = [], len(header)

    def sealed():
        encoded = header + b''.join(pending)
        filename = _publish_encoded(root, encoded, hashlib.sha256(encoded).hexdigest(), age=age)
        offset = len(header)
        for record in pending:
            yield filename, hashlib.sha256(record).hexdigest(), len(record), offset
            offset += len(record)

    for record in records:
        if pending and size + len(record) > pack_bytes:
            yield from sealed()
            pending, size = [], len(header)
        pending.append(record)
        size += len(record)
    if pending:
        yield from sealed()


def publish_many(root, configurations, *, age=None):
    """Pack small configurations without allocating a filesystem block per leaf.

    Rows own the filename, offset, byte size and digest of their exact record.
    Packs are immutable and contain at most PACK_BYTES, apart from one admitted
    larger record. No file index or shared artifact owner is needed.
    """
    def encoded_records():
        for configuration in configurations:
            if not isinstance(configuration, dict):
                raise ValueError('Recommendation configuration must be an object')
            encoded = canonical({'format': FORMAT, 'configuration': configuration})
            if len(encoded) > MAX_BYTES:
                raise ValueError('Recommendation policy exceeds its file byte budget')
            yield encoded

    yield from pack_encoded(root, encoded_records(), header=PACK_HEADER, pack_bytes=PACK_BYTES, age=age)


def repack(root, records, publisher):
    """Pack verified (value, age) records without merging distinct custody clocks.

    Source-file ordering keeps existing packs together. Grouping is lazy and the
    domain publisher retains its normal byte bound; no file-age registry is used.
    """
    for _clocks, group in groupby(records, key=lambda record: (record[1].mtime_ns, record[1].ctime_ns)):
        first, age = next(group)
        yield from publisher(root, chain((first,), (value for value, _age in group)), age=age)


def _range(offset,size):
    if (type(offset) is not int or offset < 0
            or size is not None and (type(size) is not int or not 0 < size <= MAX_BYTES)):
        raise ValueError('Invalid recommendation policy file range')


def _decoded(path,digest,encoded,offset,size):
    if (len(encoded) > MAX_BYTES or size is not None and len(encoded) != size
            or hashlib.sha256(encoded).hexdigest() != digest or not offset and path.stem != digest):
        raise ValueError('Recommendation policy file does not match its digest')
    document = json.loads(encoded)
    if (not isinstance(document, dict) or set(document) != {'format', 'configuration'}
            or document['format'] != FORMAT or not isinstance(document['configuration'], dict)):
        raise ValueError('Unsupported recommendation policy document')
    if canonical(document) != encoded:
        raise ValueError('Noncanonical recommendation policy document')
    return document['configuration']


def read(filename, digest, *, offset=0, size=None, with_age=False):
    path = Path(filename)
    _range(offset,size)
    with fao.read(path) as stream:
        age = stream.info
        if offset:
            if (size is None or offset < len(PACK_HEADER) or stream.read(len(PACK_HEADER)) != PACK_HEADER
                    or len(path.stem) != 64 or any(c not in '0123456789abcdef' for c in path.stem)):
                raise ValueError('Invalid recommendation policy pack reference')
            stream.seek(offset)
            encoded = stream.read(size)
        else:
            encoded = stream.read(MAX_BYTES + 1)
    value = _decoded(path,digest,encoded,offset,size)
    return (value,age) if with_age else value


def read_many(references, *, with_age=False):
    """Stream ordered file/range references with one pinned reader per pack.

    Each record retains the same range, digest and canonical-format checks as
    read(). Keeping adjacent offsets together avoids decompressing every prefix
    again during export and restore; memory stays bounded to one record.
    """
    for filename,records in groupby(references,key=lambda reference:reference[0]):
        path = Path(filename)
        with fao.read(path) as stream:
            header = None
            for _,digest,offset,size in records:
                _range(offset,size)
                if offset:
                    if header is None:
                        stream.seek(0)
                        header = stream.read(len(PACK_HEADER))
                    if (size is None or offset<len(PACK_HEADER) or header!=PACK_HEADER
                            or len(path.stem)!=64 or any(c not in '0123456789abcdef' for c in path.stem)):
                        raise ValueError('Invalid recommendation policy pack reference')
                    stream.seek(offset)
                    encoded = stream.read(size)
                else:
                    stream.seek(0)
                    encoded = stream.read(MAX_BYTES+1)
                value = _decoded(path,digest,encoded,offset,size)
                yield (value,stream.info) if with_age else value


def _policy_read(conn, memory_key):
    return retained_reader(directory(conn, memory_key))


def _record(version, raw, created):
    if (not isinstance(version, str) or not version or type(created) not in (int, float)
            or not math.isfinite(created) or not isinstance(raw, str)):
        raise ValueError('Invalid recommendation policy version, configuration or creation time')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Recommendation configuration must be an object')
    encoded = canonical({'format': FORMAT, 'configuration': value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Recommendation policy exceeds its file byte budget')
    return value, hashlib.sha256(encoded).hexdigest()


def install(conn, rows, bases=(), *, memory_key=None):
    """Install immutable configurations in the caller's publication transaction.

    Retain the existing compact message format at the fitter boundary. No JSON
    configuration is stored in the database. Validate the entire batch and its
    bases before publishing files, and reserve SQLite's writer slot before I/O.
    """
    if not conn.in_transaction:
        raise ValueError('Policy publication requires an outer transaction')
    conn.execute('UPDATE settings SET value=value WHERE 0')
    prepared = {owner: {} for owner in OWNERS}
    for version, raw, size, created in bases:
        value, digest = _record(version, raw, created)
        if type(size) is not int or size != len(raw.encode()):
            raise ValueError('Invalid shared policy byte size')
        previous = prepared[OWNERS[0]].get(version)
        if previous is not None and previous[1] != digest:
            raise ValueError('Conflicting shared recommendation policy version')
        prepared[OWNERS[0]][version] = (value, digest, created, None)
    for version, raw, created in rows:
        value, digest = _record(version, raw, created)
        base = value.get(BASE_FIELD)
        if base is not None and (not isinstance(base, str) or not base):
            raise ValueError('Invalid shared recommendation policy reference')
        previous = prepared[OWNERS[1]].get(version)
        if previous is not None and previous[1] != digest:
            raise ValueError('Conflicting recommendation policy version')
        prepared[OWNERS[1]][version] = (value, digest, created, base)
    pending = []
    for owner, records in prepared.items():
        for version, (_value, digest, _created, base) in records.items():
            existing = conn.execute(f'SELECT content_digest FROM {owner} WHERE version=?', (version,)).fetchone()
            if existing is not None and existing[0] != digest:
                raise ValueError(f'Conflicting immutable recommendation policy: {version!r}')
            if existing is None and owner==OWNERS[1]:
                from . import policy_history_read
                if policy_history_read.available(conn):
                    historical = policy_history_read.document(conn,version)
                    if historical is not None and historical['configuration'] != _value:
                        raise ValueError(f'Conflicting immutable historical policy: {version!r}')
            if (base is not None and base not in prepared[OWNERS[0]] and not conn.execute(
                    'SELECT 1 FROM recommendation_policy_bodies WHERE version=?', (base,)).fetchone()):
                raise ValueError(f'Missing shared recommendation policy body: {base!r}')
            if existing is None:
                pending.append((owner, version, _value, digest, _created, base))
    root = directory(conn, memory_key)
    for (owner, version, value, digest, created, base), (filename, copied, size, offset) in zip(
            pending, publish_many(root, (row[2] for row in pending))):
        if copied != digest:
            raise ValueError('Policy publication changed a configuration')
        columns = 'version,configuration_filename,content_digest,byte_size,configuration_offset,created_at'
        data = (version, filename, digest, size, offset, created)
        if owner == OWNERS[1]:
            columns += ',base_version'
            data += (base,)
        conn.execute(f'INSERT INTO {owner}({columns}) VALUES (' + ','.join('?' for _ in data)
                     + ') ON CONFLICT(version) DO NOTHING', data)
    versions = list(prepared[OWNERS[1]])
    for start in range(0, len(versions), 256):
        batch = versions[start:start + 256]
        conn.execute('UPDATE recommendation_impression_policies SET policy_version=raw_version,resolution_reason=NULL '
                     "WHERE resolution_reason='missing_policy' AND raw_version IN ("
                     + ','.join('?' for _ in batch) + ')', batch)


def configuration(conn, version, *, memory_key=None):
    """Reconstruct one policy from its exact compact variant and shared body."""
    if not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_policies'").fetchone():
        row = conn.execute('SELECT configuration FROM rec_serving_policies WHERE model_version=?', (version,)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        base = value.pop(BASE_FIELD, None)
        if base is not None:
            body = conn.execute('SELECT configuration FROM rec_policy_bases WHERE base_version=?', (base,)).fetchone()
            if body is None:
                raise ValueError(f'Missing shared recommendation policy body: {base}')
            value = {**json.loads(body[0]), **value}
        return value
    with _policy_read(conn, memory_key):
        row = conn.execute('SELECT configuration_filename,content_digest,base_version,configuration_offset,byte_size '
                           'FROM recommendation_policies WHERE version=?', (version,)).fetchone()
        if row is None:
            return None
        result = read(row[0], row[1], offset=row[3], size=row[4])
        if result.pop(BASE_FIELD, None) != row[2]:
            raise ValueError('Policy configuration does not match its shared-body reference')
        if row[2] is not None:
            body = conn.execute('SELECT configuration_filename,content_digest,configuration_offset,byte_size '
                                'FROM recommendation_policy_bodies WHERE version=?', (row[2],)).fetchone()
            if body is None:
                raise ValueError(f'Missing shared recommendation policy body: {row[2]}')
            result = {**read(body[0], body[1], offset=body[2], size=body[3]), **result}
        return result


def captured(conn, versions, *, memory_key=None):
    """Detach immutable configurations before releasing a queued offer's reader."""
    rows, bases = [], {}
    with _policy_read(conn, memory_key):
        for version in sorted(set(versions) - {None}):
            row = conn.execute('SELECT configuration_filename,content_digest,base_version,created_at,configuration_offset,byte_size '
                               'FROM recommendation_policies WHERE version=?', (version,)).fetchone()
            if row is None:
                continue
            value = read(row[0], row[1], offset=row[4], size=row[5])
            if value.get(BASE_FIELD) != row[2]:
                raise ValueError('Policy configuration does not match its shared-body reference')
            rows.append((version, canonical(value).decode(), row[3]))
            if row[2] is not None and row[2] not in bases:
                base = conn.execute('SELECT configuration_filename,content_digest,created_at,configuration_offset,byte_size '
                                    'FROM recommendation_policy_bodies WHERE version=?', (row[2],)).fetchone()
                if base is None:
                    raise ValueError(f'Missing captured policy body: {row[2]}')
                raw = canonical(read(base[0], base[1], offset=base[3], size=base[4])).decode()
                bases[row[2]] = (row[2], raw, len(raw.encode()), base[2])
    return rows, list(bases.values())


@contextmanager
def coordinate_updates(conn, owners):
    """Relocate verified files without treating their coordinates as new input.

    The caller owns the restore transaction and changes only file coordinates.
    Restoring the exact trigger definitions in that transaction preserves both
    publication guards and dependency capture, including after rollback.
    """
    if not conn.in_transaction:
        raise ValueError('Coordinate relocation requires a restore transaction')
    guards = []
    for owner in sorted(set(owners)):
        for name, sql in conn.execute(
                "SELECT name,sql FROM main.sqlite_schema WHERE type='trigger' AND tbl_name=?",
                (owner,)).fetchall():
            if 'UPDATE' in sql.upper().split(' ON ')[0]:
                guards.append(sql)
                conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    try:
        yield
    finally:
        for sql in guards:
            conn.execute(sql)


def collect_orphans(conn, *, memory_key=None):
    if conn.in_transaction:
        raise ValueError('Policy cleanup requires its own writer reservation')
    root = directory(conn, memory_key)
    if not root.exists():
        return 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        with (root / '.readers.lock').open('a+b') as lock:
            # Never wait for a reader while holding SQLite's writer slot.
            unlock = _try_file_lock(lock, exclusive=True)
            if unlock is None:
                conn.rollback()
                return 0
            try:
                retained = {Path(row[0]).resolve() for owner in OWNERS
                            for row in conn.execute(f'SELECT configuration_filename FROM {owner}')}
                removed = 0
                for path in chain(root.glob('*/*.json'), root.glob('*/.pending-*')):
                    if path.resolve() not in retained:
                        removed += fao.delete(path, missing_ok=True)
                conn.commit()
            finally:
                unlock()
        return removed
    except BaseException:
        conn.rollback()
        raise

