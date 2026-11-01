"""Retained originals belong to their catalog, artist, album or workflow owner.

Payloads are exact historical text, including non-JSON signatures. Queryable
ambiguity guards have a separate typed relation; archive age never permits GC.
"""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

from .activity import check_cancellable_work
from .evidence_ledger import canonical
from . import policy_documents

FORMAT = 'legacy-evidence-1'
MAX_BYTES = 8 * 1024 * 1024
PACK_BYTES = 256 * 1024
DOMAINS = {
    'catalog': 'legacy_catalog_evidence',
    'artist': 'artist_identity_evidence',
    'album': 'album_identity_evidence',
    'playlist': 'playlist_legacy_evidence',
    'recommendation': 'recommendation_legacy_evidence',
    'workflow': 'workflow_legacy_evidence',
    'metadata': 'metadata_legacy_evidence',
    'discovery': 'discovery_identity_evidence',
}
SUBJECTS = {
    'catalog': {'legacy_id': 'legacy_track_refs(legacy_id)', 'entry_id': 'catalog_entries(id)', 'recording_id': 'recordings(id)'},
    'artist': {'artist_id': 'artists(id)'},
    'album': {'album_id': 'albums(id)'},
    'playlist': {'playlist_scope_id': 'playlist_scopes(ref)'},
    'recommendation': {'recording_id': 'recordings(id)'},
    'workflow': {},
    'metadata': {'entry_id': 'catalog_entries(id)', 'recording_id': 'recordings(id)'},
    'discovery': {'recording_id': 'recordings(id)'},
}


def _subjects(conn, domain, kind, key, payload):
    """Only exact retained keys establish a subject; names never do."""
    result = {}
    try:
        body = json.loads(payload)
    except (ValueError, TypeError):
        body = None
    if domain == 'catalog' and kind == 'library':
        row = conn.execute('SELECT legacy_id,entry_id,recording_id FROM legacy_track_refs '
                           'WHERE CAST(legacy_id AS TEXT)=?', (key,)).fetchone()
        if row:
            result.update(zip(('legacy_id', 'entry_id', 'recording_id'), row))
    for column, owner in SUBJECTS[domain].items():
        if column in result:
            continue
        table, field = owner[:-1].split('(')
        value = body.get(column) if isinstance(body, dict) else None
        if column == 'artist_id' and kind == 'ambiguous_album_artist':
            value = key
        if value is not None:
            row = conn.execute(f'SELECT {field} FROM {table} WHERE {field}=?', (value,)).fetchone()
            if row:
                result[column] = row[0]
    return result


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='legacy_catalog_evidence'").fetchone() is not None


def root_for_database(path, memory_key):
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.archives')
    return Path(tempfile.gettempdir()) / 'tuneconsole-archives' / memory_key


def directory(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path or None, getattr(conn, '_policy_memory_key', str(id(conn))))


def archive_domain(kind):
    # Frozen relationship repair receipts can concern subjects that no longer
    # exist. Their producer still establishes the domain, without guessing a FK.
    if kind in ('constraint_legacy:playlist_items', 'constraint_legacy:unresolved_playlist_items',
                'constraint_legacy:playlists'):
        return 'playlist'
    if kind in ('constraint_legacy:road_trip_drafts', 'road_trip_drafts'):
        return 'workflow'
    if kind == 'ambiguous_album_artist' or kind.startswith(('artist_', 'artist_state:', 'artist_reconcile:')):
        return 'artist'
    if kind.startswith(('album_', 'album_legacy:')) or kind == 'ambiguous_album':
        return 'album'
    if kind.startswith(('playlist_scope_', 'playlist_')) or kind in ('playlists', 'cluster_canvas'):
        return 'playlist'
    if kind.startswith(('constraint_legacy:enrichment_', 'enrichment_', 'legacy_enrichment_')):
        return 'metadata'
    if kind == 'actions':
        return 'workflow'
    if kind.startswith('discovery_'):
        return 'discovery'
    if kind.startswith(('rec_', 'feedback_')) or kind == 'taste_modes':
        return 'recommendation'
    raise ValueError('Unclassified retained archive kind: ' + kind)


def _header(domain):
    if domain not in DOMAINS:
        raise ValueError('Unknown legacy evidence owner')
    return f'TuneConsole {domain} retained evidence 1\n'.encode()


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and not set(value) - set('0123456789abcdef')


def _encode(domain, payload, *, kind='original'):
    _header(domain)
    encoded = canonical({'format': FORMAT, 'owner': domain, 'kind': kind, 'value': payload})
    if not 0 < len(encoded) <= MAX_BYTES:
        raise ValueError('Retained original exceeds its file budget')
    return encoded


def _publish(root, domain, encoded):
    pending, size = [], len(_header(domain))

    def seal():
        packed = _header(domain) + b''.join(pending)
        filename = policy_documents._publish_encoded(root, packed, hashlib.sha256(packed).hexdigest())
        offset = len(_header(domain))
        for record in pending:
            yield filename, hashlib.sha256(record).hexdigest(), len(record), offset
            offset += len(record)

    for record in encoded:
        check_cancellable_work()
        if not isinstance(record, bytes) or not 0 < len(record) <= MAX_BYTES:
            raise ValueError('Invalid encoded retained original')
        if pending and size + len(record) > PACK_BYTES:
            yield from seal()
            pending, size = [], len(_header(domain))
        pending.append(record)
        size += len(record)
    if pending:
        yield from seal()


def _read(domain, filename, digest, size, offset, *, kind='original'):
    import json
    if (not _digest(digest) or type(size) is not int or not 0 < size <= MAX_BYTES
            or type(offset) is not int or offset < 0):
        raise ValueError('Invalid retained evidence range')
    path = Path(filename)
    if not _digest(path.stem):
        raise ValueError('Invalid retained evidence filename')
    with path.open('rb') as stream:
        if offset:
            header = _header(domain)
            if offset < len(header) or stream.read(len(header)) != header:
                raise ValueError('Retained evidence pack has a different owner')
            stream.seek(offset)
        encoded = stream.read(size + (not offset))
    if (len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest
            or not offset and path.stem != digest):
        raise ValueError('Retained evidence content changed')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or set(value) != {'format', 'owner', 'kind', 'value'}
            or value['format'] != FORMAT or value['owner'] != domain or value['kind'] != kind
            or canonical(value) != encoded):
        raise ValueError('Unsupported retained evidence format')
    return encoded, value['value']


def retain_many(conn, domain, rows):
    """Publish a bounded batch before its owning SQL rows; caller owns rollback."""
    if not conn.in_transaction:
        raise RuntimeError('Retaining legacy evidence needs an outer transaction')
    table = DOMAINS[domain]
    pending, used = [], 0
    for kind, source_id, payload, reason in rows:
        if (len(pending) == 512 or not all(isinstance(v, str) for v in (kind, source_id, payload, reason))
                or not kind or not reason):
            raise ValueError('Invalid retained evidence batch')
        encoded = _encode(domain, payload)
        used += len(encoded)
        if used > 32 * 1024 * 1024:
            raise ValueError('Retained evidence batch exceeds its memory budget')
        existing = conn.execute(f'SELECT filename,content_digest,byte_size,file_offset FROM {table} '
                                'WHERE kind=? AND source_id=?', (kind, source_id)).fetchone()
        if existing:
            # The original insert-or-ignore boundaries retain the first source
            # state. A replay does not silently overwrite historical evidence.
            _read(domain, *existing)
            continue
        pending.append((kind, source_id, reason, encoded, _subjects(conn, domain, kind, source_id, payload)))
    references = _publish(directory(conn) / domain, domain, (row[3] for row in pending))
    for (kind, source_id, reason, _, subjects), reference in zip(pending, references):
        columns = ''.join(',' + column for column in subjects)
        placeholders = ',?' * len(subjects)
        conn.execute(f'INSERT INTO {table}(kind,source_id,reason,filename,content_digest,byte_size,file_offset,format_version,'
                     f'resolution_status{columns}) VALUES (?,?,?,?,?,?,?,?,?{placeholders})',
                     (kind, source_id, reason, *reference, FORMAT, 'resolved' if subjects else 'unresolved', *subjects.values()))


def retain(conn, kind, source_id, payload, reason, *, domain=None):
    if not available(conn):
        conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
                     (kind, source_id, payload, reason))
        return
    retain_many(conn, domain or archive_domain(kind), [(kind, source_id, payload, reason)])


def original(conn, domain, kind, source_id):
    with policy_documents.retained_reader(directory(conn)):
        row = conn.execute(f'SELECT filename,content_digest,byte_size,file_offset FROM {DOMAINS[domain]} '
                           'WHERE kind=? AND source_id=?', (kind, source_id)).fetchone()
        return _read(domain, *row)[1] if row else None


def mark_artist_ambiguity(conn, artist_id):
    if available(conn):
        conn.execute('INSERT OR IGNORE INTO artist_ambiguities(artist_id,reason) VALUES (?,?)',
                     (artist_id, 'contextual_release_credit'))


def _owners(conn):
    for domain, table in DOMAINS.items():
        for row in conn.execute(f'SELECT kind,source_id,content_digest,byte_size,file_offset,filename FROM {table} '
                                'ORDER BY kind,source_id'):
            yield domain, tuple(row)


def _document(root, value, kind):
    encoded = _encode('catalog', value, kind=kind)
    digest = hashlib.sha256(encoded).hexdigest()
    return policy_documents._publish_encoded(root, encoded, digest), digest


def _read_document(path, kind):
    path = Path(path)
    return _read('catalog', str(path), path.stem, path.stat().st_size, 0, kind=kind)[1]


def export(conn, target):
    """Explicit owners from the same pinned snapshot as the SQLite backup."""
    target = Path(target)
    if not conn.in_transaction or target.resolve() == directory(conn).resolve():
        raise ValueError('Legacy evidence export needs a pinned snapshot and separate destination')
    entries, chunks = [], []

    def seal():
        if len(chunks) == 65536:
            raise ValueError('Retained evidence manifest exceeds its chunk budget')
        chunks.append(_document(target, entries, 'backup-chunk')[1])
        entries.clear()

    # One pack is resident at a time. Each owner remains distinct even when
    # two owners contain identical text; no captured event is deduplicated.
    batch = []

    def publish_batch():
        domain = batch[0][0]
        records = (_read(owner, row[5], row[2], row[3], row[4])[0] for owner, row in batch)
        for (_, row), reference in zip(batch, _publish(target / domain, domain, records)):
            filename, digest, size, offset = reference
            entries.append([domain, row[0], row[1], digest, size, offset,
                            str(Path(filename).relative_to(target))])
            if len(entries) == 512:
                seal()
        batch.clear()

    for domain, row in _owners(conn):
        check_cancellable_work()
        if batch and (domain != batch[0][0] or len(batch) == 128):
            publish_batch()
        batch.append((domain, row))
    if batch:
        publish_batch()
    if entries:
        seal()
    return _document(target, {'chunks': chunks}, 'backup-manifest')[0]


def restore(conn, manifest):
    """Validate every original and publish all files before relocating SQL owners."""
    if conn.in_transaction:
        raise ValueError('Legacy evidence restore needs an idle connection')
    path = Path(manifest)
    root = path.parent.parent
    document = _read_document(path, 'backup-manifest')
    if (not isinstance(document, dict) or set(document) != {'chunks'}
            or not isinstance(document['chunks'], list) or len(document['chunks']) > 65536
            or any(not _digest(value) for value in document['chunks'])):
        raise ValueError('Invalid retained evidence manifest')
    with closing(sqlite3.connect('')) as staged:
        staged.execute('CREATE TABLE owners(domain TEXT,kind TEXT,source_id TEXT,digest TEXT,size INTEGER,'
                       'offset INTEGER,filename TEXT,target TEXT,target_offset INTEGER,'
                       'PRIMARY KEY(domain,kind,source_id)) WITHOUT ROWID')
        for digest in document['chunks']:
            entries = _read_document(root / digest[:2] / (digest + '.json'), 'backup-chunk')
            if not isinstance(entries, list) or not 1 <= len(entries) <= 512:
                raise ValueError('Invalid retained evidence backup chunk')
            for entry in entries:
                if (not isinstance(entry, list) or len(entry) != 7 or entry[0] not in DOMAINS
                        or not all(isinstance(v, str) for v in (entry[1], entry[2], entry[6]))):
                    raise ValueError('Invalid retained evidence backup owner')
                relative = Path(entry[6])
                if (len(relative.parts) != 3 or relative.parts[0] != entry[0]
                        or not _digest(relative.stem) or relative.suffix != '.json'
                        or relative.parts[1] != relative.stem[:2]):
                    raise ValueError('Invalid retained evidence backup path')
                source = str(root / relative)
                _read(entry[0], source, entry[3], entry[4], entry[5])
                staged.execute('INSERT INTO owners(domain,kind,source_id,digest,size,offset,filename) '
                               'VALUES (?,?,?,?,?,?,?)', (*entry[:6], source))
        conn.execute('BEGIN IMMEDIATE')
        try:
            for domain, table in DOMAINS.items():
                expected = iter(staged.execute('SELECT kind,source_id,digest,size FROM owners '
                                              'WHERE domain=? ORDER BY kind,source_id', (domain,)))
                for actual in conn.execute(f'SELECT kind,source_id,content_digest,byte_size FROM {table} '
                                           'ORDER BY kind,source_id'):
                    if tuple(actual) != next(expected, None):
                        raise ValueError('Retained backup differs from its database owner')
                if next(expected, None) is not None:
                    raise ValueError('Retained backup has an extra database owner')
            for domain in DOMAINS:
                after = None
                while True:
                    batch = staged.execute('SELECT kind,source_id,digest,size,offset,filename FROM owners WHERE domain=?'
                        + (' AND (kind,source_id)>(?,?)' if after is not None else '')
                        + ' ORDER BY kind,source_id LIMIT 128', (domain, *after) if after is not None else (domain,)).fetchall()
                    if not batch:
                        break
                    records = (_read(domain, row[5], row[2], row[3], row[4])[0] for row in batch)
                    for row, reference in zip(batch, _publish(directory(conn) / domain, domain, records)):
                        staged.execute('UPDATE owners SET target=?,target_offset=? WHERE domain=? AND kind=? AND source_id=?',
                                       (reference[0], reference[3], domain, row[0], row[1]))
                    after = batch[-1][:2]
            for domain, table in DOMAINS.items():
                for kind, source_id, filename, offset in staged.execute('SELECT kind,source_id,target,target_offset '
                                                                       'FROM owners WHERE domain=?', (domain,)):
                    conn.execute(f'UPDATE {table} SET filename=?,file_offset=? WHERE kind=? AND source_id=?',
                                 (filename, offset, kind, source_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def collect_orphans(conn):
    """Only unowned files can expire; retained originals have no age cutoff."""
    if conn.in_transaction:
        raise ValueError('Archive cleanup needs its own writer reservation')
    root = directory(conn)
    policy_documents._mkdir(root)
    conn.execute('BEGIN IMMEDIATE')
    try:
        with (root / '.readers.lock').open('a+b') as lock:
            unlock = policy_documents._try_file_lock(lock, exclusive=True)
            if unlock is None:
                conn.rollback()
                return 0
            try:
                removed = 0
                for domain, table in DOMAINS.items():
                    for path in (root / domain).glob('*/*.json'):
                        check_cancellable_work()
                        if not conn.execute(f'SELECT 1 FROM {table} WHERE filename=? LIMIT 1', (str(path),)).fetchone():
                            path.unlink()
                            policy_documents._sync_directory(path.parent)
                            removed += 1
                conn.commit()
                return removed
            finally:
                unlock()
    except BaseException:
        conn.rollback()
        raise
