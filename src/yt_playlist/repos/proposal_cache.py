"""Immutable cache chunks and a small, atomically published serving reference."""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import time

from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.db_checkpoint import checkpoint_wal, deferred_autocheckpoint
from yt_playlist.core.db_diagnostics import database_operation
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.core.db_space import require_database_space

CHUNK_BYTES = 64 * 1024
SURFACES = ('mode_bundles', 'fresh_pool')
TABLES = frozenset({'rec_proposal_cache_heads', 'rec_proposal_cache_builds', 'rec_proposal_cache_chunks'})
LEASE_SECONDS = 60


@dataclass(frozen=True)
class PreparedCache:
    surface: str
    cache_id: str


@contextmanager
def _write(repo):
    committed, checkpoint = False, False
    try:
        with database_operation('RecProposalsRepo.stage'), repo._lock:
            check_cancellable_work()
            if repo.conn.in_transaction:
                raise RuntimeError('Recommendation cache staging requires an idle serving connection')
            with deferred_autocheckpoint(repo.conn, repo._store._db_path is not None) as checkpoint:
                with repo.conn:
                    repo.conn.execute('BEGIN IMMEDIATE')
                    yield repo.conn
                committed = True
    finally:
        if checkpoint and committed:
            checkpoint_wal(repo._store._db_path)


def _reads(store, table, key):
    return keyed_reads(store, table, (key,)) if store is not None else nullcontext()


def head(conn, surface, *, store=None):
    with _reads(store, 'rec_proposal_cache_heads', surface):
        row = conn.execute('SELECT cache_id,built_at FROM rec_proposal_cache_heads WHERE surface=?',
                           (surface,)).fetchone()
    if row is None:
        return None
    with _reads(store, 'rec_proposal_cache_builds', row[0]):
        build = conn.execute('SELECT cache_id,byte_size,chunk_count,nonempty FROM rec_proposal_cache_builds '
                             'WHERE cache_id=? AND state=1', (row[0],)).fetchone()
    if build is None:
        raise ValueError('Published recommendation cache is incomplete')
    return build


def read(conn, surface, *, store=None):
    build = head(conn, surface, store=store)
    if build is None:
        return None
    cache_id, byte_size, count, _nonempty = build
    with _reads(store, 'rec_proposal_cache_chunks', cache_id):
        rows = conn.execute('SELECT chunk_index,payload FROM rec_proposal_cache_chunks '
                            'WHERE cache_id=? ORDER BY chunk_index', (cache_id,)).fetchall()
    if len(rows) != count or any(row[0] != index for index, row in enumerate(rows)):
        raise ValueError('Published recommendation cache has missing chunks')
    raw = b''.join(row[1] for row in rows)
    if len(raw) != byte_size or _identity(surface, raw) != cache_id:
        raise ValueError('Published recommendation cache bytes do not match its identity')
    return raw.decode('utf-8')


def logical_rows(conn, *, store=None):
    """Detach active payloads; the retained legacy row is hidden after cutover.

    Policy recovery also uses this helper on databases predating this migration.
    Its unannotated reads guard whole tables. Repositories supply their reader
    Store to guard individual immutable builds and both possible head keys.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='rec_proposal_cache_heads'").fetchone() is None:
        return [{'surface': surface, 'payload': payload} for surface, payload in
                conn.execute('SELECT surface,payload FROM rec_proposals')]
    rows = [{'surface': surface, 'payload': payload} for surface, payload in conn.execute(
        "SELECT surface,payload FROM rec_proposals WHERE surface NOT IN ('mode_bundles','fresh_pool')")]
    for surface in SURFACES:
        payload = read(conn, surface, store=store)
        if payload is None:
            with _reads(store, 'rec_proposals', surface):
                row = conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
            payload = row[0] if row else None
        if payload is not None:
            rows.append({'surface': surface, 'payload': payload})
    return rows


def dependencies(conn):
    """Known cache keys for a bounded snapshot, captured while its source lock is held."""
    result = {('rec_proposal_cache_heads', 'key:' + surface) for surface in SURFACES}
    result.update(('rec_proposals', 'key:' + surface) for surface in SURFACES)
    for row in conn.execute('SELECT cache_id FROM rec_proposal_cache_heads'):
        result.update((table, 'key:' + row[0]) for table in
                      ('rec_proposal_cache_builds', 'rec_proposal_cache_chunks'))
    return result


def _identity(surface, raw):
    digest = hashlib.sha256(surface.encode() + b'\0')
    digest.update(raw)
    return digest.hexdigest()


def cleanup(repo, *, cache_id=None, exclude=None, before=None):
    """Remove at most one chunk per transaction; active and leased builds survive."""
    with _write(repo) as conn:
        row = conn.execute('SELECT cache_id FROM rec_proposal_cache_builds b WHERE '
            '(state=2 OR expires_at<?) AND NOT EXISTS '
            '(SELECT 1 FROM rec_proposal_cache_heads h WHERE h.cache_id=b.cache_id) '
            + ('AND cache_id=? ' if cache_id is not None else '')
            + ('AND cache_id<>? ' if exclude is not None else '')
            + 'ORDER BY expires_at LIMIT 1',
            (time.time() if before is None else before,)
            + ((cache_id,) if cache_id is not None else ())
            + ((exclude,) if exclude is not None else ())).fetchone()
        if row is None:
            return False
        selected = row[0]
        conn.execute('UPDATE rec_proposal_cache_builds SET state=2,expires_at=0 WHERE cache_id=?', (selected,))
        deleted = conn.execute('DELETE FROM rec_proposal_cache_chunks WHERE cache_id=? AND chunk_index='
            '(SELECT chunk_index FROM rec_proposal_cache_chunks WHERE cache_id=? ORDER BY chunk_index LIMIT 1)',
            (selected, selected)).rowcount
        if not deleted:
            conn.execute('DELETE FROM rec_proposal_cache_builds WHERE cache_id=?', (selected,))
        return True


def stage(repo, surface, encoded):
    """Resume a durable contiguous prefix; expose nothing until finish()."""
    if surface not in SURFACES:
        raise ValueError(f'Unsupported staged recommendation surface: {surface}')
    if repo._lock.held_by_current_thread:
        raise RuntimeError('Recommendation cache staging must run outside the serving lock')
    raw = encoded.encode('utf-8')
    if not raw:
        raise ValueError('Recommendation cache payload is empty')
    cache_id = _identity(surface, raw)
    count = (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
    while cleanup(repo, exclude=cache_id):
        check_cancellable_work()
    while True:
        with _write(repo) as conn:
            row = conn.execute('SELECT state FROM rec_proposal_cache_builds WHERE cache_id=?',
                               (cache_id,)).fetchone()
            if row is None:
                require_database_space(conn, 2 * len(raw))
                conn.execute('INSERT INTO rec_proposal_cache_builds '
                    '(cache_id,surface,byte_size,chunk_count,nonempty,created_at,expires_at) VALUES (?,?,?,?,?,?,?)',
                    (cache_id, surface, len(raw), count, int(encoded not in ('null', '{}')),
                     time.time(), time.time() + LEASE_SECONDS))
            elif row[0] != 2:
                # Do not mutate an active build just to reuse it: a snapshot may
                # depend on its immutable header as well as its chunks.
                conn.execute('UPDATE rec_proposal_cache_builds SET expires_at=? WHERE cache_id=? '
                    'AND NOT EXISTS(SELECT 1 FROM rec_proposal_cache_heads WHERE cache_id=?)',
                    (time.time() + LEASE_SECONDS, cache_id, cache_id))
            cleaning = row is not None and row[0] == 2
        if not cleaning:
            break
        while cleanup(repo, cache_id=cache_id):
            check_cancellable_work()
    while True:
        with _write(repo) as conn:
            row = conn.execute('SELECT written_chunks,state FROM rec_proposal_cache_builds WHERE cache_id=?',
                               (cache_id,)).fetchone()
            if row is None or row[1] == 2:
                raise WorkBudgetExceeded('Recommendation cache lease expired during staging; retry preparation')
            if row[1] == 1:
                return PreparedCache(surface, cache_id)
            offset = row[0]
            if offset == count:
                conn.execute('UPDATE rec_proposal_cache_builds SET state=1 WHERE cache_id=?', (cache_id,))
            else:
                require_database_space(conn, 2 * (len(raw) - offset * CHUNK_BYTES))
                conn.execute('INSERT INTO rec_proposal_cache_chunks VALUES (?,?,?)',
                             (cache_id, offset, raw[offset * CHUNK_BYTES:(offset + 1) * CHUNK_BYTES]))
            conn.execute('UPDATE rec_proposal_cache_builds SET expires_at=? WHERE cache_id=?',
                         (time.time() + LEASE_SECONDS, cache_id))


def finish(conn, prepared, now=None):
    """The guarded publisher owns this small switch and its policy/settings writes."""
    if not conn.in_transaction:
        raise RuntimeError('Recommendation cache publication requires an outer transaction')
    if conn.execute('SELECT 1 FROM rec_proposal_cache_builds WHERE cache_id=? AND surface=? AND state=1',
                    (prepared.cache_id, prepared.surface)).fetchone() is None:
        raise WorkBudgetExceeded('Prepared recommendation cache expired before publication; retry preparation')
    if now is None:
        row = conn.execute('SELECT built_at FROM rec_proposal_cache_heads WHERE surface=? UNION ALL '
                           'SELECT built_at FROM rec_proposals WHERE surface=? LIMIT 1',
                           (prepared.surface, prepared.surface)).fetchone()
        now = row[0] if row else None
    conn.execute('INSERT OR IGNORE INTO rec_proposals(surface,payload,built_at) VALUES (?,\'null\',?)',
                 (prepared.surface, now))
    conn.execute('INSERT INTO rec_proposal_cache_heads VALUES (?,?,?) ON CONFLICT(surface) '
                 'DO UPDATE SET cache_id=excluded.cache_id,built_at=excluded.built_at',
                 (prepared.surface, prepared.cache_id, now))
