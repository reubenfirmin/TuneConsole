"""Retained originals belong to their catalog, artist, album or workflow owner.

Payloads are exact historical text, including non-JSON signatures. Queryable
ambiguity guards have a separate typed relation. Cold journals own explicit
bounded diagnostic retention and durable user/paid/scope recovery exceptions.
"""
import hashlib
from . import fao
import json
from contextlib import closing
from itertools import islice
from pathlib import Path
import sqlite3
import tempfile

from .activity import check_cancellable_work
from .evidence_ledger import canonical
from . import policy_documents
from .legacy_evidence_contract import (FORMAT, MAX_BYTES, DOMAINS, SUBJECTS,
    subjects as _subjects, header as _header, valid_digest as _digest, read as _read)

PACK_BYTES = 256 * 1024


def available(conn):
    from . import legacy_journals
    if legacy_journals.available(conn):
        return True
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


def _encode(domain, payload, *, kind='original'):
    _header(domain)
    encoded = canonical({'format': FORMAT, 'owner': domain, 'kind': kind, 'value': payload})
    if not 0 < len(encoded) <= MAX_BYTES:
        raise ValueError('Retained original exceeds its file budget')
    return encoded


def _publish(root, domain, encoded, *, age=None):
    def validated_records():
        for record in encoded:
            check_cancellable_work()
            if not isinstance(record, bytes) or not 0 < len(record) <= MAX_BYTES:
                raise ValueError('Invalid encoded retained original')
            yield record

    yield from policy_documents.pack_encoded(root, validated_records(),
        header=_header(domain), pack_bytes=PACK_BYTES, age=age)


def retain_many(conn, domain, rows):
    """Publish a bounded batch before its owning SQL rows; caller owns rollback."""
    if not conn.in_transaction:
        raise RuntimeError('Retaining legacy evidence needs an outer transaction')
    from . import legacy_journals
    if legacy_journals.available(conn):
        pending = []
        table = DOMAINS[domain]
        installed = conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(table,)).fetchone()
        for row in rows:
            existing = conn.execute(f'SELECT filename,content_digest,byte_size,file_offset FROM {table} '
                'WHERE kind=? AND source_id=?',row[:2]).fetchone() if installed else None
            if existing:
                _read(domain,*existing)
                continue
            pending.append(row)
            if len(pending) == 128:
                legacy_journals.retain_many(conn,domain,pending)
                pending.clear()
        if pending:
            legacy_journals.retain_many(conn,domain,pending)
        return
    stage_many(conn, domain, rows)


def stage_many(conn, domain, rows):
    """Capture exact receipts for later bounded journal publication.

    Bulk importers can retain a complete batch before choosing archive order.
    Readers already support these receipts alongside a published journal;
    prepare_batch transfers them atomically after the import has finished.
    """
    if not conn.in_transaction:
        raise RuntimeError('Retaining legacy evidence needs an outer transaction')
    from . import legacy_journals
    rows = list(islice(rows, 513))
    if len(rows) > 512:
        raise ValueError('Invalid retained evidence batch')
    archived = {}
    if legacy_journals.available(conn) and legacy_journals.head(conn, domain) is not None:
        from . import file_map
        archived = file_map.get_many(legacy_journals.directory(conn) / domain,
            legacy_journals.head(conn, domain), [legacy_journals.key(row[0], row[1]) for row in rows])
    table = DOMAINS[domain]
    pending, used, selected = [], 0, set()
    for kind, source_id, payload, reason in rows:
        if (len(pending) == 512 or not all(isinstance(v, str) for v in (kind, source_id, payload, reason))
                or not kind or not reason):
            raise ValueError('Invalid retained evidence batch')
        encoded = _encode(domain, payload)
        used += len(encoded)
        if used > 32 * 1024 * 1024:
            raise ValueError('Retained evidence batch exceeds its memory budget')
        if legacy_journals.key(kind, source_id) in archived:
            continue
        if (kind, source_id) in selected:
            continue
        existing = conn.execute(f'SELECT filename,content_digest,byte_size,file_offset FROM {table} '
                                'WHERE kind=? AND source_id=?', (kind, source_id)).fetchone()
        if existing:
            # The original insert-or-ignore boundaries retain the first source
            # state. A replay does not silently overwrite historical evidence.
            _read(domain, *existing)
            continue
        pending.append((kind, source_id, reason, encoded, _subjects(conn, domain, kind, source_id, payload)))
        selected.add((kind, source_id))
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
    from . import legacy_journals
    if legacy_journals.available(conn):
        saved = legacy_journals.read(conn,domain,kind,source_id)
        if saved is not None:
            return saved.get('payload')
        if not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(DOMAINS[domain],)).fetchone():
            return None
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
        if not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(table,)).fetchone():
            continue
        for row in conn.execute(f'SELECT kind,source_id,content_digest,byte_size,file_offset,filename FROM {table} '
                                'ORDER BY filename,file_offset,kind,source_id'):
            yield domain, tuple(row)


def _document(root, value, kind):
    encoded = _encode('catalog', value, kind=kind)
    digest = hashlib.sha256(encoded).hexdigest()
    return policy_documents._publish_encoded(root, encoded, digest), digest


def _read_document(path, kind):
    path = Path(path)
    return _read('catalog', str(path), path.stem, fao.stat(path).logical_bytes, 0, kind=kind)[1]


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
        records = ((value[0], age) for owner, row in batch
                   for value, age in [_read(owner, row[5], row[2], row[3], row[4], with_age=True)])
        def publish(root, values, *, age):
            return _publish(root, domain, values, age=age)
        for (_, row), reference in zip(batch, policy_documents.repack(target / domain, records, publish)):
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
        staged.execute('CREATE INDEX owners_source ON owners(domain,filename,offset,kind,source_id)')
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
                        + (' AND (filename,offset,kind,source_id)>(?,?,?,?)' if after is not None else '')
                        + ' ORDER BY filename,offset,kind,source_id LIMIT 128',
                        (domain, *after) if after is not None else (domain,)).fetchall()
                    if not batch:
                        break
                    records = ((value[0], age) for row in batch
                               for value, age in [_read(domain, row[5], row[2], row[3], row[4], with_age=True)])
                    def publish(root, values, *, age):
                        return _publish(root, domain, values, age=age)
                    for row, reference in zip(batch, policy_documents.repack(directory(conn) / domain, records, publish)):
                        staged.execute('UPDATE owners SET target=?,target_offset=? WHERE domain=? AND kind=? AND source_id=?',
                                       (reference[0], reference[3], domain, row[0], row[1]))
                    after = batch[-1][5], batch[-1][4], *batch[-1][:2]
            for domain, table in DOMAINS.items():
                if not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(table,)).fetchone():
                    continue
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
                    present = conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(table,)).fetchone()
                    for path in (root / domain).glob('*/*.json'):
                        check_cancellable_work()
                        if not present or not conn.execute(f'SELECT 1 FROM {table} WHERE filename=? LIMIT 1', (str(path),)).fetchone():
                            removed += fao.delete(path, missing_ok=True)
                conn.commit()
                return removed
            finally:
                unlock()
    except BaseException:
        conn.rollback()
        raise
