"""Bounded group writes, with a last-good head and leased unpublished builds."""
from contextlib import contextmanager
import hashlib
from itertools import islice
import json
import time
import uuid

from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.db_checkpoint import checkpoint_wal, deferred_autocheckpoint
from yt_playlist.core.db_diagnostics import database_operation

BATCH_ROWS = 128
BATCH_BYTES = 256 * 1024
MAX_ROW_BYTES = 8 * 1024 * 1024
LEASE_SECONDS = 60
# Published history is immutable in SQLite. Appending hidden staging rows cannot
# change these snapshot inputs; the publication marker and head remain guarded.
STAGED_TABLES = frozenset({'rec_group_generations', 'rec_group_definitions', 'rec_group_members',
    'rec_group_identities', 'rec_group_unknown', 'rec_group_lineage', 'rec_group_graph', 'rec_group_builds'})
_CLEANUP_OWNER = 'cleanup'


class _BuildBeingCleaned(Exception):
    pass


class BuildUnavailable(WorkBudgetExceeded):
    """A retained definition or live lease still belongs to another build."""


@contextmanager
def _write(repo):
    committed, checkpoint = False, False
    try:
        with database_operation('TasteGroupsRepo.stage'), repo._lock:
            check_cancellable_work()
            if repo.conn.in_transaction:
                raise RuntimeError('Group staging requires an idle serving connection')
            with deferred_autocheckpoint(repo.conn, repo._store._db_path is not None) as checkpoint:
                with repo.conn:
                    repo.conn.execute('BEGIN IMMEDIATE')
                    yield repo.conn
                committed = True
    finally:
        if checkpoint and committed:
            checkpoint_wal(repo._store._db_path)


def _batches(rows):
    batch, size = [], 0
    for row in rows:
        check_cancellable_work()
        used = sum(len(value.encode('utf-8')) if isinstance(value, str) else 8 for value in row)
        if used > MAX_ROW_BYTES:
            raise WorkBudgetExceeded('Musical group publication row exceeds byte budget')
        if batch and (len(batch) >= BATCH_ROWS or size + used > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += used
    if batch:
        yield batch


def _touch(conn, generation, owner):
    row = conn.execute('UPDATE rec_group_builds SET expires_at=? WHERE generation=? '
                       'AND owner=? AND ready=0 RETURNING generation',
                       (time.time() + LEASE_SECONDS, generation, owner)).fetchone()
    if row is None:
        raise RuntimeError('Musical group build is no longer owned by this worker')


def generation_row(prepared):
    return (prepared['generation'], prepared['policy'], prepared['fingerprint'],
            prepared['at'], prepared['serial'], prepared['payload'])


def _definitions(prepared):
    return ((g['version'], prepared['generation'], g['group_id'], g['parent_version'], g['payload'],
             g.get('legacy_mode_id'), prepared['at']) for g in prepared['groups'])


def _operations(prepared):
    generation = prepared['generation']
    return (
        ('INSERT INTO rec_group_members VALUES (?,?,?,?)',
         ((g['version'], *member) for g in prepared['groups'] for member in g['members'])),
        ('INSERT INTO rec_group_lineage VALUES (?,?,?,?)', iter(prepared['lineage'])),
        ('INSERT INTO rec_group_unknown VALUES (?,?,?)', ((generation, *r) for r in prepared['unknown'])),
        ('INSERT INTO rec_group_graph VALUES (?,?,?,?,?)', ((generation, *r) for r in prepared['edges'])),
    )


def _header(prepared):
    # A deterministic input identity alone is insufficient after code changes.
    # Retain a digest of every ordered output row before reusing any prefix.
    if '_stage_digest' not in prepared:
        digest = hashlib.sha256()
        for rows in ([generation_row(prepared)], _definitions(prepared),
                     *(rows for _sql, rows in _operations(prepared))):
            for batch in _batches(rows):
                for row in batch:
                    digest.update(json.dumps(row, separators=(',', ':'), allow_nan=False).encode())
                    digest.update(b'\n')
        prepared['_stage_digest'] = digest.hexdigest()
    payload = {**json.loads(prepared['payload']), '_staging_digest': prepared['_stage_digest'],
               '_staging_parent': prepared.get('_stage_parent')}
    return (*generation_row(prepared)[:-1], json.dumps(payload, separators=(',', ':'), sort_keys=True))


def _claim(repo, prepared):
    generation = prepared['generation']
    owner = prepared.setdefault('_stage_owner', uuid.uuid4().hex)
    header = next(_batches([_header(prepared)]))
    with _write(repo) as conn:
        existing = conn.execute('SELECT generation,policy,fingerprint,created_at,serial,payload '
                                'FROM rec_group_generations WHERE generation=?', (generation,)).fetchone()
        if existing is None:
            conn.executemany('INSERT INTO rec_group_generations VALUES (?,?,?,?,?,?)', header)
            conn.execute('INSERT INTO rec_group_builds VALUES (?,?,?,0)',
                         (generation, owner, time.time() + LEASE_SECONDS))
            return owner, False
        build = conn.execute('SELECT owner,expires_at,ready FROM rec_group_builds WHERE generation=?',
                             (generation,)).fetchone()
        if build is None:
            raise RuntimeError('Cannot resume a published musical group generation')
        if build['owner'] == _CLEANUP_OWNER:
            raise _BuildBeingCleaned
        if tuple(existing) != tuple(header[0]):
            raise RuntimeError('Musical group resume plan does not match its retained output digest')
        if build['owner'] != owner and build['expires_at'] > time.time():
            raise BuildUnavailable('Musical group build is leased by another worker')
        conn.execute('UPDATE rec_group_builds SET owner=?,expires_at=? WHERE generation=?',
                     (owner, time.time() + LEASE_SECONDS, generation))
        return owner, bool(build['ready'])


def _counts(repo, generation):
    # Prefix lengths are durable cursors: each batch commits all of its rows or
    # none. Count through a private reader, never under the serving lock.
    with repo._store.read_snapshot() as snapshot:
        conn = snapshot.store.conn
        statements = (
            'SELECT COUNT(*) FROM rec_group_definitions WHERE generation=?',
            'SELECT COUNT(*) FROM rec_group_definitions d CROSS JOIN rec_group_members m '
            'ON m.version=d.version WHERE d.generation=?',
            'SELECT COUNT(*) FROM rec_group_definitions d CROSS JOIN rec_group_lineage l '
            'ON l.target=d.version WHERE d.generation=?',
            'SELECT COUNT(*) FROM rec_group_unknown WHERE generation=?',
            'SELECT COUNT(*) FROM rec_group_graph WHERE generation=?',
        )
        return [conn.execute(sql, (generation,)).fetchone()[0] for sql in statements]


def _clear_legacy_conflicts(repo, prepared):
    # A prior interrupted plan can own a globally stable legacy version. It
    # must be cleaned through its expired lease, never silently ignored or
    # reassigned while another worker or retained history still owns it.
    with repo._store.read_snapshot() as snapshot:
        conn = snapshot.store.conn
        conflicts = set()
        for group in prepared['groups']:
            if group.get('legacy_mode_id') is None:
                continue
            row = conn.execute('SELECT generation FROM rec_group_definitions WHERE version=? '
                               'AND generation<>?', (group['version'], prepared['generation'])).fetchone()
            if row is not None:
                conflicts.add(row[0])
    for generation in sorted(conflicts):
        while cleanup(repo, generation=generation):
            check_cancellable_work()
        with repo._store.read_snapshot() as snapshot:
            if snapshot.store.conn.execute('SELECT 1 FROM rec_group_definitions WHERE generation=? LIMIT 1',
                                           (generation,)).fetchone() is not None:
                raise BuildUnavailable('Legacy group definition is still owned by another build or publication')


def stage(repo, prepared):
    generation = prepared['generation']
    while True:
        try:
            owner, ready = _claim(repo, prepared)
            break
        except _BuildBeingCleaned:
            while cleanup(repo, generation=generation):
                check_cancellable_work()
    if ready:
        return owner
    _clear_legacy_conflicts(repo, prepared)
    counts = _counts(repo, generation)
    expected = [len(prepared['groups']), sum(len(g['members']) for g in prepared['groups']),
                len(prepared['lineage']), len(prepared['unknown']), len(prepared['edges'])]
    if any(count > limit for count, limit in zip(counts, expected)):
        raise RuntimeError('Musical group staged prefix exceeds its retained plan')
    # Identities and their definitions commit together, so cleanup never needs
    # to guess whether an orphan identity belongs to an active build.
    for batch in _batches(islice(_definitions(prepared), counts[0], None)):
        with _write(repo) as conn:
            _touch(conn, generation, owner)
            conn.executemany('INSERT OR IGNORE INTO rec_group_identities VALUES (?,?,?)',
                             [(r[2], r[5], r[6]) for r in batch])
            conn.executemany('INSERT INTO rec_group_definitions VALUES (?,?,?,?,?)', [r[:5] for r in batch])
    for (sql, rows), offset in zip(_operations(prepared), counts[1:]):
        for batch in _batches(islice(rows, offset, None)):
            with _write(repo) as conn:
                _touch(conn, generation, owner)
                conn.executemany(sql, batch)
    with _write(repo) as conn:
        _touch(conn, generation, owner)
        conn.execute('UPDATE rec_group_builds SET ready=1 WHERE generation=?', (generation,))
    return owner


def abandon(repo, prepared):
    if (owner := prepared.get('_stage_owner')) is not None:
        with _write(repo) as conn:
            conn.execute('UPDATE rec_group_builds SET expires_at=0 WHERE generation=? AND owner=?',
                         (prepared['generation'], owner))


def finish(conn, generation, owner=None):
    if not conn.in_transaction:
        raise RuntimeError('Group publication requires an outer transaction')
    if owner is not None:
        row = conn.execute('SELECT 1 FROM rec_group_builds WHERE generation=? AND owner=? AND ready=1',
                           (generation, owner)).fetchone()
        if row is None:
            raise RuntimeError('Musical group build is incomplete or no longer owned')
    conn.execute('INSERT INTO rec_group_publications VALUES (?)', (generation,))
    conn.execute('INSERT INTO rec_group_head VALUES (1,?) ON CONFLICT(id) '
                 'DO UPDATE SET generation=excluded.generation', (generation,))
    conn.execute('DELETE FROM rec_group_builds WHERE generation=?', (generation,))


def cleanup(repo, *, before=None, generation=None):
    """One bounded deletion; published definitions, members and lineage remain immutable."""
    with _write(repo) as conn:
        row = conn.execute('SELECT b.generation FROM rec_group_builds b '
            'WHERE expires_at<? AND NOT EXISTS (SELECT 1 FROM rec_group_publications p '
            'WHERE p.generation=b.generation) ' + ('AND b.generation=? ' if generation is not None else '') +
            'ORDER BY expires_at LIMIT 1',
            (time.time() if before is None else before,) + ((generation,) if generation is not None else ())).fetchone()
        if row is not None:
            generation = row[0]
            # Once any prefix has been removed, its remaining row count is no
            # longer a resume cursor. Transfer ownership to cleanup first and
            # finish removing this generation before it can be rebuilt.
            conn.execute('UPDATE rec_group_builds SET owner=?,ready=0,expires_at=0 WHERE generation=?',
                         (_CLEANUP_OWNER, generation))
            statements = (
                ('rec_group_members', '(version,identity_key)', 'SELECT m.version,m.identity_key '
                 'FROM rec_group_definitions d CROSS JOIN rec_group_members m ON m.version=d.version '
                 'WHERE d.generation=? LIMIT ?'),
                ('rec_group_lineage', '(source,target)', 'SELECT l.source,l.target '
                 'FROM rec_group_definitions d CROSS JOIN rec_group_lineage l ON l.target=d.version '
                 'WHERE d.generation=? LIMIT ?'),
                ('rec_group_unknown', '(generation,identity_key)',
                 'SELECT generation,identity_key FROM rec_group_unknown WHERE generation=? LIMIT ?'),
                ('rec_group_graph', '(generation,left_key,right_key)',
                 'SELECT generation,left_key,right_key FROM rec_group_graph WHERE generation=? LIMIT ?'),
            )
            for table, key, selected in statements:
                if conn.execute(f'DELETE FROM {table} WHERE {key} IN ({selected})',
                                (generation, BATCH_ROWS)).rowcount:
                    return True
            definitions = conn.execute('SELECT version,group_id FROM rec_group_definitions d '
                'WHERE generation=? AND NOT EXISTS (SELECT 1 FROM rec_group_definitions child '
                'WHERE child.parent_version=d.version) LIMIT ?', (generation, BATCH_ROWS)).fetchall()
            if definitions:
                conn.executemany('DELETE FROM rec_group_definitions WHERE version=?', [(r[0],) for r in definitions])
                conn.executemany('DELETE FROM rec_group_identities WHERE group_id=? AND NOT EXISTS '
                    '(SELECT 1 FROM rec_group_definitions WHERE group_id=?)', [(r[1], r[1]) for r in definitions])
                return True
            conn.execute('DELETE FROM rec_group_builds WHERE generation=?', (generation,))
            conn.execute('DELETE FROM rec_group_generations WHERE generation=?', (generation,))
            return True
        if generation is not None:
            return False
        # Graphs and provisional assignments were always disposable caches.
        # Readers that pinned the prior head retain their consistent WAL view.
        for table, key in (('rec_group_graph', '(generation,left_key,right_key)'),
                           ('rec_group_assignments', '(generation,identity_key)')):
            old = conn.execute('SELECT p.generation FROM rec_group_publications p '
                'WHERE NOT EXISTS (SELECT 1 FROM rec_group_head h WHERE h.generation=p.generation) '
                f'AND EXISTS (SELECT 1 FROM {table} t WHERE t.generation=p.generation) LIMIT 1').fetchone()
            if old is not None:
                conn.execute(f'DELETE FROM {table} WHERE {key} IN (SELECT {key[1:-1]} FROM {table} '
                             'WHERE generation=? LIMIT ?)', (old[0], BATCH_ROWS))
                return True
        return False
