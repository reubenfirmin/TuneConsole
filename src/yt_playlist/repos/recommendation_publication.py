"""Leased bounded preparation and one guarded recommendation head switch."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, astuple, fields
import time
import uuid

from yt_playlist.core import recommendation_chunks, recommendation_pools as pools
from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.db_checkpoint import checkpoint_wal, deferred_autocheckpoint
from yt_playlist.core.db_diagnostics import database_operation
from yt_playlist.core.db_writes import reserved_write
from yt_playlist.core.recommendation_build_state import builds_with_pool, stage_pool
from yt_playlist.core.recommendation_custody import retain, release
from yt_playlist.core.recommendation_plan import (
    BATCH_ROWS, Build, Framing, MAX_PLAN_BYTES, MAX_SLOTS, OUTPUT_BATCH_ROWS, Output, Plan,
    SLOT_BATCH_ROWS, Slot, finite as _finite, validate,
)

CLEANUP_ROWS = 1024
CLEANUP_POOLS = 32
LEASE_SECONDS = 60
FRAMING_COLUMNS = ','.join(field.name for field in fields(Framing))


@contextmanager
def staging_write(repo):
    committed, deferred = False, False
    try:
        with database_operation('RecProposalsRepo.stage'), repo._lock:
            check_cancellable_work()
            if repo.conn.in_transaction:
                raise RuntimeError('Recommendation staging requires an idle serving connection')
            with deferred_autocheckpoint(repo.conn, repo._store._db_path is not None) as deferred:
                with repo.conn:
                    repo.conn.execute('BEGIN IMMEDIATE')
                    yield repo.conn
                committed = True
    finally:
        if deferred and committed:
            checkpoint_wal(repo._store._db_path)


def _touch(conn, build):
    if not conn.execute('UPDATE recommendation_generations SET build_expires_at=? WHERE id=? AND build_owner=? '
            "AND output_digest=? AND status IN ('building','ready')",
            (time.time() + LEASE_SECONDS, build.generation_id, build.owner, build.plan_digest)).rowcount:
        raise WorkBudgetExceeded('Recommendation build was superseded or retired')


def _create(conn, plan, digest):
    token = uuid.uuid4().hex
    generation = conn.execute('INSERT INTO recommendation_generations(external_key,policy_version,'
        'group_generation_id,output_digest,bundle_required,input_revision,revision_reason,created_at,build_owner,build_expires_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id', (plan.external_key, plan.policy_version, plan.group_generation_id,
        digest, int(plan.framing is not None), plan.input_revision, 'legacy_not_recorded' if plan.input_revision is None else None,
        plan.created_at, token, time.time() + LEASE_SECONDS)).fetchone()[0]
    counts = Counter(slot.surface for slot in plan.slots)
    files = Counter(output.surface for output in plan.outputs)
    conn.executemany('INSERT INTO recommendation_generation_surfaces(generation_id,surface,pool_count,output_count) VALUES (?,?,?,?)',
                     [(generation, surface, counts[surface], files[surface]) for surface in sorted(counts.keys() | files.keys())])
    return Build(generation, token, digest)


def claim(repo, plan, digest, *, owner=None):
    with staging_write(repo) as conn:
        previous = conn.execute('SELECT id,output_digest,status,build_owner,build_expires_at '
                                'FROM recommendation_generations WHERE external_key=?',
                                (plan.external_key,)).fetchone()
        if previous:
            generation, output_digest, status, lease_owner, expires_at = previous
            if output_digest != digest:
                raise ValueError('Recommendation generation changed its immutable plan')
            if status in ('published', 'retiring'):
                raise WorkBudgetExceeded('Recommendation generation has already been published or retired')
            if lease_owner is None or lease_owner == 'cleanup':
                raise WorkBudgetExceeded('Recommendation generation is awaiting cleanup')
            if owner != lease_owner and expires_at > time.time():
                raise WorkBudgetExceeded('Recommendation generation is leased by another worker')
            token = owner if owner == lease_owner else uuid.uuid4().hex
            conn.execute('UPDATE recommendation_generations SET build_owner=?,build_expires_at=? WHERE id=?',
                         (token, time.time() + LEASE_SECONDS, generation))
            return Build(generation, token, digest)
        return _create(conn, plan, digest)


def _create_pool(conn, prepared):
    return conn.execute('INSERT INTO recommendation_pools(pool_key,policy_version,'
        'input_fingerprint,content_digest,purpose,eligible_count) VALUES (?,?,?,?,?,?) RETURNING id',
        (prepared.pool_key, prepared.policy_version, prepared.input_fingerprint,
         prepared.content_digest, prepared.purpose, len(prepared.candidates))).fetchone()[0]


def begin_pool(repo, build, prepared):
    with staging_write(repo) as conn:
        _touch(conn, build)
        row = conn.execute('SELECT id,complete,retiring FROM recommendation_pools WHERE content_digest=?',
                            (prepared.content_digest,)).fetchone()
        if row is None:
            identity = _create_pool(conn, prepared)
            stage_pool(conn, build.generation_id, identity)
            pools.append(conn, identity, prepared.candidates[:BATCH_ROWS], 0)
            count = min(BATCH_ROWS, len(prepared.candidates))
            sealed = False
        else:
            identity, sealed, retiring = row
            count = None
            if retiring:
                raise WorkBudgetExceeded('Recommendation pool is awaiting bounded cleanup')
            if sealed:
                stage_pool(conn, build.generation_id, identity)
            else:
                owner = conn.execute(builds_with_pool('?'), (identity,)).fetchone()
                if owner is None or owner[0] != build.generation_id:
                    raise WorkBudgetExceeded('Recommendation pool belongs to another unfinished build')
    return identity, bool(sealed), count


def _output(conn, plan, generation, output, filename, file_identity):
    pool_id = None
    if output.pool_digest is not None:
        pool_id = conn.execute('SELECT id FROM recommendation_pools WHERE content_digest=?',
                                (output.pool_digest,)).fetchone()[0]
    recommendation_chunks.attach(conn, file_identity, generation, filename, plan.created_at, output.expires_at,
                                 pool_id, output.first_rank, output.last_rank)


def _ready(conn, plan, generation):
    staged = conn.execute(f'SELECT status,{FRAMING_COLUMNS} FROM recommendation_generations WHERE id=?',
                          (generation,)).fetchone()
    status = staged[0]
    if plan.framing:
        values = astuple(plan.framing)
        if staged[1] is None:
            marks = ','.join('?' for _ in values)
            conn.execute(f'UPDATE recommendation_generations SET ({FRAMING_COLUMNS})=({marks}) WHERE id=?',
                         (*values, generation))
        elif staged[1:] != values:
            raise ValueError('Recommendation staged bundle framing changed')
    if status == 'building':
        conn.execute("UPDATE recommendation_generations SET status='ready' WHERE id=?", (generation,))


def finish(conn, build, at):
    """Switch the committed recommendation head in its repository's SQL write."""
    if not conn.in_transaction or not _finite(at):
        raise RuntimeError('Recommendation publication requires an outer transaction and finite time')
    _touch(conn, build)
    if conn.execute("SELECT 1 FROM recommendation_generations WHERE id=? AND staged_pool_ids<>'[]'",
                    (build.generation_id,)).fetchone():
        raise ValueError('Recommendation publication has unfinished pools')
    updated = conn.execute("UPDATE recommendation_generations SET status='published',published_at=?,"
                           'build_owner=NULL,build_expires_at=NULL '
                           "WHERE id=? AND status='ready'", (at, build.generation_id)).rowcount
    if not updated:
        raise WorkBudgetExceeded('Recommendation generation is not ready to publish')
    conn.execute('UPDATE recommendation_generation_surfaces SET selected=0 WHERE selected=1 AND surface IN '
                 '(SELECT surface FROM recommendation_generation_surfaces WHERE generation_id=?)', (build.generation_id,))
    conn.execute('UPDATE recommendation_generation_surfaces SET selected=1 WHERE generation_id=?', (build.generation_id,))


@dataclass(frozen=True)
class Retirement:
    rows: int = 0
    generations: int = 0
    pools: int = 0

    def __bool__(self):
        return bool(self.rows or self.generations or self.pools)


def _delete(conn, table, columns, predicate, parameters, limit):
    keys = ','.join(columns)
    return conn.execute(f'DELETE FROM {table} WHERE ({keys}) IN (SELECT {keys} FROM {table} WHERE '
                        + predicate + ' LIMIT ?)', (*parameters, limit)).rowcount


def cleanup(repo):
    """Give generations and orphan pools independent bounded progress."""
    generations = cleanup_generations(repo)
    pools = cleanup_pools(repo)
    return bool(generations or pools)


def _retiring_generation(conn):
    from yt_playlist.core.recommendation_custody import HOME_GENERATIONS

    return conn.execute('SELECT g.id,g.status FROM recommendation_generations g WHERE NOT EXISTS('
        'SELECT 1 FROM recommendation_generation_surfaces s WHERE s.generation_id=g.id AND s.selected=1) '
        f"AND g.evidence_roots='[]' AND g.id NOT IN ({HOME_GENERATIONS}) AND ("
        "g.status IN ('published','retiring') OR g.build_expires_at<?) ORDER BY g.id LIMIT 1", (time.time(),)).fetchone()


def _orphan_pools():
    return ('SELECT p.id FROM recommendation_pools p WHERE NOT EXISTS('
        'SELECT 1 FROM recommendation_surface_pools m WHERE m.pool_id=p.id) AND NOT EXISTS('
        + builds_with_pool('p.id') + ') AND NOT EXISTS('
        f'SELECT 1 FROM {recommendation_chunks.VIEW} o WHERE o.pool_id=p.id) ORDER BY p.id LIMIT ?')


def retirement_pending(conn):
    """Use the collectors' actual owners, including pools detached from a deleted generation."""
    return bool(_retiring_generation(conn) or conn.execute(_orphan_pools(), (1,)).fetchone())


@contextmanager
def _retirement_write(repo, checkpoint):
    """Wait for another writer outside ServingLock; commit one short prefix."""
    committed = deferred = False
    try:
        with reserved_write(repo._store):
            with deferred_autocheckpoint(repo.conn, repo._store._db_path is not None) as deferred:
                yield repo.conn
                repo.conn.commit()
                committed = True
    finally:
        if checkpoint and deferred and committed:
            repo.checkpoint_retirement()


def cleanup_generations(repo, *, checkpoint=True, row_limit=CLEANUP_ROWS):
    """Fence a no-longer-required generation, then remove a bounded prefix."""
    with _retirement_write(repo, checkpoint) as conn:
        row = _retiring_generation(conn)
        if row:
            identity, status = row
            removed = 0
            if status != 'retiring':
                conn.execute("UPDATE recommendation_generations SET status='retiring',"
                             "build_owner=CASE WHEN build_owner IS NOT NULL THEN 'cleanup' END,"
                             'build_expires_at=CASE WHEN build_owner IS NOT NULL THEN 0 END WHERE id=?', (identity,))
            removed = recommendation_chunks.detach(conn, identity, row_limit)
            for table, columns in (('recommendation_surface_pools', ('generation_id', 'surface', 'mode_key', 'purpose')),
                                   ('recommendation_generation_surfaces', ('generation_id', 'surface'))):
                if removed == row_limit:
                    return Retirement(rows=removed)
                removed += _delete(conn, table, columns, 'generation_id=?', (identity,), row_limit - removed)
            if removed == row_limit:
                return Retirement(rows=removed)
            conn.execute('DELETE FROM recommendation_generations WHERE id=?', (identity,))
            return Retirement(rows=removed, generations=1)
        return Retirement()


def cleanup_pools(repo, *, checkpoint=True, row_limit=CLEANUP_ROWS, pool_limit=CLEANUP_POOLS):
    """Drain orphan pools even while generations remain eligible for retirement."""
    removed = retired = 0
    with _retirement_write(repo, checkpoint) as conn:
        # The writer reservation keeps this bounded selection valid throughout
        # the transaction. Repeating the same anti-joins for each small pool
        # can consume the whole turn once retained pools lead the ID order.
        selected = tuple(row[0] for row in conn.execute(_orphan_pools(), (pool_limit,)))
        if not selected:
            return Retirement()
        marks = ','.join('?' for _ in selected)
        conn.execute('UPDATE recommendation_pools SET retiring=1 WHERE retiring=0 AND id IN (' + marks + ')', selected)
        check_cancellable_work()
        removed = _delete(conn, 'recommendation_candidates', ('pool_id', 'rank'),
                          'pool_id IN (' + marks + ')', selected, row_limit)
        # Delete empty parents together, even when another selected pool still
        # has children to drain next time. The reservation and retirement fence
        # keep the owner decision valid through this commit.
        empty = tuple(row[0] for row in conn.execute(
            'SELECT p.id FROM recommendation_pools p WHERE p.id IN (' + marks + ') AND '
            'NOT EXISTS(SELECT 1 FROM recommendation_candidates c WHERE c.pool_id=p.id)', selected))
        if empty:
            marks = ','.join('?' for _ in empty)
            retired = conn.execute('DELETE FROM recommendation_pools WHERE id IN (' + marks + ')', empty).rowcount
    return Retirement(rows=removed, pools=retired)
