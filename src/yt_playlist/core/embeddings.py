"""Canonical embedding owners and atomic publication of a single basis per space.

Text keys are accepted at the repository boundary. They are resolved before a
write; stored relationships and joins always use recording or artist IDs.
"""
import hashlib
import json
import math
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass

from . import artists, embedding_files


# These names are the fitter's existing message fields, not SQL identifiers.
SPACES = {
    'rec_vectors': ('recording', 'collaborative'),
    'rec_content_vectors': ('recording', 'content_library'),
    'rec_discovered_content_vectors': ('recording', 'content_discovery'),
    'rec_artist_vectors': ('artist', 'collaborative'),
    'rec_artist_content_vectors': ('artist', 'content'),
}


def descriptor(name):
    try:
        kind, space = SPACES[name]
    except KeyError:
        raise ValueError('Invalid training model table') from None
    return kind, space, f'{kind}_embeddings', f'{kind}_id'


def version(name, settings):
    """Identify the actual encoder/objective, rather than just its dimensions."""
    kind, space, _table, _column = descriptor(name)
    if space.startswith('content'):
        raw = settings.get('rec_content_model' if kind == 'recording' else 'rec_artist_content_model')
        if not raw:
            return None
        basis = ['embedding-basis-1', kind, 'content', json.loads(raw)]
    else:
        inputs = settings.get('rec_fit_inputs' if kind == 'recording' else 'rec_artist_fit_inputs')
        if not inputs:
            return None
        basis = ['embedding-basis-1', kind, space, inputs,
                 (settings.get('rec_embed_method') or 'svd') if kind == 'recording' else 'svd',
                 (settings.get('rec_dim') or '48') if kind == 'recording' else '48']
    return hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def dimensions(vector):
    if not isinstance(vector, bytes) or not vector or len(vector) % 4:
        raise ValueError('An embedding must contain complete float32 values')
    if any(not math.isfinite(value) for value, in struct.iter_unpack('=f', vector)):
        raise ValueError('An embedding must contain finite float32 values')
    return len(vector) // 4


def model_dimensions(name, settings):
    kind, space, _table, _column = descriptor(name)
    if not space.startswith('content'):
        return None
    raw = settings.get('rec_content_model' if kind == 'recording' else 'rec_artist_content_model')
    if not raw:
        return None
    model = json.loads(raw)
    return model['ncat'] + len(model['cont'])


def head(conn, name):
    kind, space, table, _column = descriptor(name)
    table = embedding_files.relation(conn, kind)
    row = conn.execute(f'SELECT model_version,dimensions FROM {table} WHERE space=? LIMIT 1',
                       (space,)).fetchone()
    return tuple(row) if row else None


def rows(conn, name, *, keys=None):
    kind, space, table, column = descriptor(name)
    table = embedding_files.relation(conn, kind)
    if kind == 'recording':
        subject = 'recordings'
        key = 's.identity_key'
        retired = 'recording_redirects'
    else:
        subject = 'artists'
        key = "'@artist:'||s.id"
        retired = 'artist_redirects'
    source = (f'{subject} s CROSS JOIN {table} e ON s.id=e.{column}' if keys is not None else
              f'{table} e JOIN {subject} s ON s.id=e.{column}')
    sql = (f'SELECT {key},e.vector,e.dimensions,e.publication,e.position FROM {source} '
           f'WHERE e.space=? AND NOT EXISTS(SELECT 1 FROM {retired} d WHERE d.retired_id=s.id)')
    args = [space]
    if keys is not None:
        keys = list(dict.fromkeys(keys))
        if not keys:
            return []
        if kind == 'recording':
            sql += ' AND s.identity_key IN (' + ','.join('?' for _ in keys) + ')'
            args.extend(keys)
        else:
            subjects = [int(key[8:]) for key in keys
                        if isinstance(key, str) and key.startswith('@artist:') and key[8:].isdigit()]
            if not subjects:
                return []
            sql += ' AND s.id IN (' + ','.join('?' for _ in subjects) + ')'
            args.extend(subjects)
    # Source insertion order is part of the discovered cohort's tied rankings.
    sql += ' ORDER BY e.insertion_order' if space == 'content_discovery' else f' ORDER BY {key}'
    return embedding_files.load_many(conn, kind, conn.execute(sql, args))


def count(conn, name):
    kind, space, table, _column = descriptor(name)
    table = embedding_files.relation(conn, kind)
    return conn.execute(f'SELECT COUNT(*) FROM {table} WHERE space=?', (space,)).fetchone()[0]


def owner(conn, kind, key):
    if kind == 'recording':
        row = conn.execute('SELECT r.id FROM recordings r WHERE r.identity_key=? '
                           'AND NOT EXISTS(SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id)',
                           (key,)).fetchone()
        subject = row[0] if row else None
    else:
        subject = artists.lookup(conn, key)
        if isinstance(key, int) or str(key).startswith('@artist:'):
            try:
                supplied = int(str(key).removeprefix('@artist:'))
            except ValueError:
                supplied = None
            if supplied != subject:
                subject = None
    if subject is None:
        raise sqlite3.IntegrityError(f'Unknown current {kind} owner: {key!r}')
    return subject


@dataclass(frozen=True)
class Publication:
    name: str
    model_version: str
    rows: tuple
    removed: tuple
    replace: bool
    now: float
    dimensions: int | None
    files: tuple | None


def prepare(conn, name, upserts, removed=(), *, replace=False, model_version=None, now=None):
    """Resolve owners, ordering and immutable bytes on the guarded reader."""
    kind, space, table, column = descriptor(name)
    admitted, seen, size = [], set(), None
    for key, vector in upserts:
        subject = owner(conn, kind, key)
        if subject in seen:
            raise ValueError('Duplicate training vector owner')
        seen.add(subject)
        dim = dimensions(vector)
        if size is not None and dim != size:
            raise ValueError('Mixed dimensions in an embedding space')
        size = dim
        admitted.append((subject, vector))
    previous = head(conn, name)
    model_version = model_version or (previous[0] if previous and not replace else 'manual:' + uuid.uuid4().hex)
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError('Embedding publication time must be finite')
    retracted = []
    for key in removed:
        # A retired owner can be retracted without resolving it to a survivor.
        if kind == 'recording':
            row = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            subject = row[0] if row else None
        else:
            subject = int(key[8:]) if str(key).startswith('@artist:') else artists.lookup(conn, key)
        retracted.append(subject)
    files = None
    if admitted and embedding_files.available(conn):
        logical = embedding_files.relation(conn, kind)
        orders, order = {}, 0
        if not replace:
            subjects = [subject for subject, _vector in admitted]
            qs = ','.join('?' for _ in subjects)
            orders = dict(conn.execute(f'SELECT {column},insertion_order FROM {logical} '
                f'WHERE space=? AND {column} IN ({qs})', (space, *subjects)))
            removed_subjects = [subject for subject in retracted if subject is not None]
            excluded = (f' AND {column} NOT IN (' + ','.join('?' for _ in removed_subjects) + ')'
                        if removed_subjects else '')
            order = conn.execute(f'SELECT COALESCE(MAX(insertion_order),0) FROM {logical} '
                'WHERE space=?' + excluded, (space, *removed_subjects)).fetchone()[0]
        # Retractions happen before appending new rows, as in the writer API.
        for subject in retracted:
            orders.pop(subject, None)
        values = []
        for subject, vector in admitted:
            previous_order = orders.get(subject)
            if previous_order is None:
                order += 1
            values.append((subject, vector, now, previous_order if previous_order is not None else order))
        files = embedding_files.prepare(conn, kind, size, values)
    return Publication(name, model_version, tuple(admitted), tuple(retracted), replace, now, size, files)


def publish_prepared(conn, publication):
    """Only SQL references change after the snapshot has been revalidated."""
    if not conn.in_transaction:
        raise RuntimeError('Embedding publication requires an outer transaction')
    kind, space, table, column = descriptor(publication.name)
    file_owned = embedding_files.available(conn)
    if publication.replace:
        conn.execute(f'DELETE FROM {table} WHERE space=?', (space,))
        if file_owned:
            conn.execute(f'DELETE FROM {kind}_embedding_files WHERE space=?', (space,))
    removed = [(subject, space) for subject in publication.removed]
    conn.executemany(f'DELETE FROM {table} WHERE {column}=? AND space=?', removed)
    if file_owned:
        conn.executemany(f'DELETE FROM {kind}_embedding_files WHERE {column}=? AND space=?', removed)
    if publication.files is not None:
        embedding_files.publish_prepared(conn, kind, space, publication.model_version, publication.files)
        return
    conn.executemany(f'INSERT INTO {table}({column},space,model_version,dimensions,vector,built_at) '
                     f'VALUES (?,?,?,?,?,?) ON CONFLICT({column},space,model_version) '
                     'DO UPDATE SET vector=excluded.vector,built_at=excluded.built_at',
                     ((subject, space, publication.model_version, publication.dimensions, vector, publication.now)
                      for subject, vector in publication.rows))


def publish(conn, name, upserts, removed=(), *, replace=False, model_version=None, now=None):
    """Caller owns the transaction; validate before removing the previous basis."""
    if not conn.in_transaction:
        raise RuntimeError('Embedding publication requires an outer transaction')
    publish_prepared(conn, prepare(conn, name, upserts, removed,
                                  replace=replace, model_version=model_version, now=now))
