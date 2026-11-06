"""Immutable responses owned directly by metadata deliveries, including backup.

Small responses share bounded packs. SQLite owns every retained range; files
contain no mutable index and never establish provider delivery identity.
"""
from contextlib import closing
import hashlib
from itertools import chain, groupby
import json
from pathlib import Path
import sqlite3
import tempfile

from .activity import check_cancellable_work
from .evidence_ledger import canonical, _sync_directory
from . import policy_documents, fao

FORMAT = 'metadata-delivery-1'
MAX_BYTES = 8 * 1024 * 1024
PACK_BYTES = 256 * 1024
PACK_HEADER = b'TuneConsole metadata deliveries 1\n'
CHUNK_ROWS = 512
MAX_CHUNKS = 65536


def root_for_database(path, memory_key):
    if path is not None:
        database = Path(path).resolve()
        return database.with_name(database.name + '.metadata')
    return Path(tempfile.gettempdir()) / 'tuneconsole-metadata' / memory_key


def directory(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path or None,
        getattr(conn, '_policy_memory_key', f'{id(conn)}'))


def encode(value, *, kind='response'):
    encoded = canonical({'format': FORMAT, 'kind': kind, 'value': value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Metadata delivery exceeds its raw response byte budget')
    return encoded


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and not set(value) - set('0123456789abcdef')


def publish_encoded(root, records, *, age=None):
    """Yield (filename, record digest, byte size, offset), retaining one pack."""
    def validated_records():
        for record in records:
            check_cancellable_work()
            if not isinstance(record, bytes) or not 0 < len(record) <= MAX_BYTES:
                raise ValueError('Invalid encoded metadata delivery')
            yield record

    yield from policy_documents.pack_encoded(root, validated_records(),
        header=PACK_HEADER, pack_bytes=PACK_BYTES, age=age)


def _range(path, digest, byte_size, offset):
    if (not _digest(digest) or type(byte_size) is not int or not 0 < byte_size <= MAX_BYTES
            or type(offset) is not int or offset < 0):
        raise ValueError('Invalid metadata delivery file range')
    if not _digest(path.stem):
        raise ValueError('Invalid metadata delivery filename')


def _decoded(path, digest, byte_size, offset, encoded, kind):
    if (len(encoded) != byte_size or hashlib.sha256(encoded).hexdigest() != digest
            or not offset and path.stem != digest):
        raise ValueError('Metadata delivery file does not match its owned content')
    document = json.loads(encoded)
    if (not isinstance(document, dict) or set(document) != {'format', 'kind', 'value'}
            or document['format'] != FORMAT or document['kind'] != kind
            or canonical(document) != encoded):
        raise ValueError('Unsupported or noncanonical metadata delivery document')
    return encoded


def _read(filename, digest, byte_size, offset, *, kind='response', with_age=False):
    path = Path(filename)
    _range(path, digest, byte_size, offset)
    with fao.open_reader(path) as stream:
        age = stream.info
        if offset:
            if offset < len(PACK_HEADER) or stream.read(len(PACK_HEADER)) != PACK_HEADER:
                raise ValueError('Invalid metadata delivery pack')
            stream.seek(offset)
        encoded = stream.read(byte_size + (not offset))
    _decoded(path, digest, byte_size, offset, encoded, kind)
    return (encoded, age) if with_age else encoded


def _read_records(references):
    """Validate ordered ranges with one pinned reader and one record in memory."""
    for filename, records in groupby(references, key=lambda reference: reference[0]):
        path = Path(filename)
        with fao.open_reader(path) as stream:
            header = None
            for reference in records:
                _, digest, size, offset = reference
                _range(path, digest, size, offset)
                if offset:
                    if header is None:
                        stream.seek(0)
                        header = stream.read(len(PACK_HEADER))
                    if offset < len(PACK_HEADER) or header != PACK_HEADER:
                        raise ValueError('Invalid metadata delivery pack')
                stream.seek(offset)
                encoded = stream.read(size + (not offset))
                yield reference, _decoded(path, digest, size, offset, encoded, 'response')


def read(filename, digest, byte_size, offset):
    return json.loads(_read(filename, digest, byte_size, offset))['value']


def references(conn):
    from . import metadata_locator_contract as metadata_locators
    if metadata_locators.retired(conn):
        return iter(())
    return conn.execute('SELECT id,raw_filename,raw_digest,raw_byte_size,raw_offset '
                        'FROM metadata_deliveries WHERE raw_filename IS NOT NULL ORDER BY id')


def owns_file(conn, filename):
    """Check one collector candidate against the admitted metadata format."""
    from . import metadata_locator_contract as metadata_locators
    if metadata_locators.retired(conn):
        return False
    return conn.execute('SELECT 1 FROM metadata_deliveries WHERE raw_filename=? LIMIT 1',
                        (str(filename),)).fetchone() is not None


def _document(root, value, kind):
    encoded = encode(value, kind=kind)
    digest = hashlib.sha256(encoded).hexdigest()
    return policy_documents._publish_encoded(root, encoded, digest), digest


def _read_document(path, kind):
    path = Path(path)
    # stat bounds allocation, and _read rejects any concurrent size change.
    return json.loads(_read(path, path.stem, fao.stat(path).logical_bytes, 0, kind=kind))['value']


def export(conn, root):
    """Caller holds the reader fence and the SQLite backup's pinned snapshot."""
    return _export(conn, root, directory=directory, references=references, contents=_contents)


def _contents(conn):
    from . import metadata_locator_contract as metadata_locators
    if metadata_locators.retired(conn):
        return iter(())
    return conn.execute('SELECT DISTINCT raw_filename,raw_digest,raw_byte_size,raw_offset '
                        'FROM metadata_deliveries WHERE raw_filename IS NOT NULL ORDER BY raw_filename,raw_offset')


def _export(conn, root, *, directory, references, contents):
    """Share the bounded range codec with other explicit metadata file owners."""
    root = Path(root)
    if not conn.in_transaction or root.resolve() == directory(conn).resolve():
        raise ValueError('Metadata export needs a pinned view and a separate directory')
    with closing(sqlite3.connect('')) as staged:
        staged.execute('PRAGMA cache_size=-1024')
        staged.execute('CREATE TABLE contents(digest TEXT,filename TEXT,size INTEGER,offset INTEGER,'
                       'target TEXT,target_offset INTEGER,PRIMARY KEY(digest,filename)) WITHOUT ROWID')
        for (filename, digest, size, offset), encoded in _read_records(contents(conn)):
            check_cancellable_work()
            old = staged.execute('SELECT filename,size,offset FROM contents WHERE digest=? AND filename=?',
                                 (digest, filename)).fetchone()
            if old:
                if _read(old[0], digest, old[1], old[2]) != encoded:
                    raise ValueError('Metadata backup content digest collision')
            else:
                staged.execute('INSERT INTO contents(digest,filename,size,offset) VALUES (?,?,?,?)',
                               (digest, filename, size, offset))
        # Backups retain existing bounded packs and their original offsets.
        # Repacking belongs to restoration/conversion, not recovery custody.
        staged.execute('CREATE INDEX contents_source ON contents(filename,offset)')
        for filename, in staged.execute('SELECT DISTINCT filename FROM contents ORDER BY filename'):
            source = Path(filename)
            relative = Path(source.stem[:2], source.stem + '.json')
            copied = root / relative
            fao.copy(source, copied, digest=source.stem)
            staged.execute('UPDATE contents SET target=?,target_offset=offset WHERE filename=?',
                           (str(relative), filename))
        entries, chunks = [], []

        def chunk():
            if len(chunks) == MAX_CHUNKS:
                raise ValueError('Metadata backup manifest exceeds its chunk budget')
            chunks.append(_document(root, entries, 'backup-chunk')[1])
            entries.clear()

        for identity, source, digest, size, _ in references(conn):
            check_cancellable_work()
            filename, offset = staged.execute('SELECT target,target_offset FROM contents '
                'WHERE digest=? AND filename=? AND size=?', (digest, source, size)).fetchone()
            entries.append({'id': identity, 'digest': digest, 'byte_size': size, 'offset': offset,
                            'relative_filename': filename})
            if len(entries) == CHUNK_ROWS:
                chunk()
        if entries:
            chunk()
        return _document(root, {'chunks': chunks}, 'backup-manifest')[0]


def _copy_contents(staged, root):
    """Publish responses per source file, preserving distinct custody clocks."""
    staged.execute('CREATE INDEX IF NOT EXISTS contents_source ON contents(filename,offset,digest)')
    after = None
    while True:
        batch = staged.execute('SELECT digest,filename,size,offset FROM contents'
            + (' WHERE (filename,offset,digest)>(?,?,?)' if after is not None else '')
            + ' ORDER BY filename,offset,digest LIMIT 128',
            after if after is not None else ()).fetchall()
        if not batch:
            return
        records = (_read(filename, digest, size, offset, with_age=True) for digest, filename, size, offset in batch)
        for row, target in zip(batch, policy_documents.repack(root, records, publish_encoded)):
            filename, digest, size, offset = target
            if (digest, size) != (row[0], row[2]):
                raise ValueError('Metadata copy changed captured content')
            staged.execute('UPDATE contents SET target=?,target_offset=? WHERE digest=? AND filename=?',
                           (filename, offset, digest, row[1]))
        after = batch[-1][1], batch[-1][3], batch[-1][0]


def _source(root, entry):
    if (not isinstance(entry, dict)
            or set(entry) != {'id', 'digest', 'byte_size', 'offset', 'relative_filename'}
            or type(entry['id']) is not int or not _digest(entry['digest'])
            or not isinstance(entry['relative_filename'], str)):
        raise ValueError('Invalid metadata backup owner')
    relative = Path(entry['relative_filename'])
    if (len(relative.parts) != 2 or not _digest(relative.stem) or relative.suffix != '.json'
            or relative.parts[0] != relative.stem[:2]):
        raise ValueError('Invalid metadata backup filename')
    path = root / relative
    _read(path, entry['digest'], entry['byte_size'], entry['offset'])
    return str(path)


def restore(conn, manifest):
    """Verify all sources before relocation, with bounded Python memory.

    A private temporary SQLite file holds the owner map, rather than one Python
    dictionary per library delivery. No source owner changes until all target
    files have also been durably published.
    """
    return _restore(conn, manifest, directory=directory, references=references, relocate=_relocate)


def _restore(conn, manifest, *, directory, references, relocate):
    if conn.in_transaction:
        raise ValueError('Metadata restore requires an idle connection')
    path = Path(manifest)
    root = path.parent.parent
    document = _read_document(path, 'backup-manifest')
    if (not isinstance(document, dict) or set(document) != {'chunks'}
            or not isinstance(document['chunks'], list)
            or len(document['chunks']) > MAX_CHUNKS
            or any(not _digest(value) for value in document['chunks'])):
        raise ValueError('Invalid metadata backup manifest')
    with closing(sqlite3.connect('')) as staged:
        staged.execute('PRAGMA cache_size=-1024')
        staged.execute('CREATE TABLE owners(id INTEGER PRIMARY KEY,filename TEXT NOT NULL,digest TEXT NOT NULL,'
                       'size INTEGER NOT NULL,offset INTEGER NOT NULL)')
        staged.execute('CREATE TABLE contents(digest TEXT,filename TEXT,size INTEGER,offset INTEGER,'
                       'target TEXT,target_offset INTEGER,PRIMARY KEY(digest,filename)) WITHOUT ROWID')
        for digest in document['chunks']:
            check_cancellable_work()
            entries = _read_document(root / digest[:2] / (digest + '.json'), 'backup-chunk')
            if not isinstance(entries, list) or not 1 <= len(entries) <= CHUNK_ROWS:
                raise ValueError('Invalid metadata backup chunk')
            for entry in entries:
                source = _source(root, entry)
                try:
                    staged.execute('INSERT INTO owners(id,filename,digest,size,offset) VALUES (?,?,?,?,?)',
                                   (entry['id'], source, entry['digest'], entry['byte_size'], entry['offset']))
                except sqlite3.IntegrityError as exc:
                    raise ValueError('Duplicate metadata backup owner') from exc
                old = staged.execute('SELECT filename,size,offset FROM contents WHERE digest=? AND filename=?',
                                     (entry['digest'], source)).fetchone()
                if old:
                    if _read(old[0], entry['digest'], old[1], old[2]) != _read(source, entry['digest'], entry['byte_size'], entry['offset']):
                        raise ValueError('Metadata restore content digest collision')
                else:
                    staged.execute('INSERT INTO contents(digest,filename,size,offset) VALUES (?,?,?,?)',
                                   (entry['digest'], source, entry['byte_size'], entry['offset']))
        conn.execute('BEGIN IMMEDIATE')
        try:
            actual = iter(references(conn))
            for expected in staged.execute('SELECT id,digest,size FROM owners ORDER BY id'):
                row = next(actual, None)
                if row is None or (row[0], row[2], row[3]) != expected:
                    raise ValueError('Metadata backup does not match database owners')
            if next(actual, None) is not None:
                raise ValueError('Metadata backup is missing a database owner')
            _copy_contents(staged, directory(conn))
            # Every target is durable before any DB owner is relocated.
            relocate(conn, staged)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _relocate(conn, staged):
    for identity, filename, offset in staged.execute('SELECT o.id,c.target,c.target_offset '
                                                    'FROM owners o JOIN contents c ON c.digest=o.digest '
                                                    'AND c.filename=o.filename ORDER BY o.id'):
        check_cancellable_work()
        conn.execute('UPDATE metadata_deliveries SET raw_filename=?,raw_offset=? WHERE id=?',
                     (filename, offset, identity))


def collect_orphans(conn):
    if conn.in_transaction:
        raise ValueError('Metadata cleanup requires an idle connection')
    root = directory(conn)
    if not root.exists():
        return 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        with (root / '.readers.lock').open('a+b') as lock:
            unlock = policy_documents._try_file_lock(lock, exclusive=True)
            if unlock is None:
                return 0
            try:
                removed = 0
                for path in chain(root.glob('*/*.json'), root.glob('*/.pending-*')):
                    check_cancellable_work()
                    if not owns_file(conn, path):
                        removed += fao.delete(path, missing_ok=True)
                return removed
            finally:
                unlock()
    finally:
        conn.rollback()
