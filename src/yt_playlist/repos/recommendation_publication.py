"""Leased bounded preparation and one guarded recommendation head switch."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, astuple
import time
import uuid

from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.db_checkpoint import deferred_autocheckpoint
from yt_playlist.core.db_writes import reserved_write
from yt_playlist.core.recommendation_custody import retain, release
from yt_playlist.core.recommendation_plan import (
    BATCH_ROWS, Build, Framing, MAX_PLAN_BYTES, MAX_SLOTS, OUTPUT_BATCH_ROWS, Output, Plan,
    SLOT_BATCH_ROWS, Slot, finite as _finite, validate,
)
from . import proposal_cache

CLEANUP_ROWS = 1024
CLEANUP_POOLS = 32
LEASE_SECONDS = 60


def _touch(conn, build):
    if not conn.execute('UPDATE recommendation_builds SET expires_at=? WHERE generation_id=? AND owner=? '
            'AND plan_digest=? AND EXISTS(SELECT 1 FROM recommendation_generations g '
            "WHERE g.id=generation_id AND g.status IN ('building','ready'))",
            (time.time() + LEASE_SECONDS, build.generation_id, build.owner, build.plan_digest)).rowcount:
        raise WorkBudgetExceeded('Recommendation build was superseded or retired')


def _create(conn, plan, digest):
    generation = conn.execute('INSERT INTO recommendation_generations(external_key,policy_version,'
        'group_generation_id,output_digest,bundle_required,input_revision,revision_reason,created_at) '
        'VALUES (?,?,?,?,?,?,?,?) RETURNING id', (plan.external_key, plan.policy_version, plan.group_generation_id,
        digest, int(plan.framing is not None), plan.input_revision, 'legacy_not_recorded' if plan.input_revision is None else None,
        plan.created_at)).fetchone()[0]
    token = uuid.uuid4().hex
    conn.execute('INSERT INTO recommendation_builds VALUES (?,?,?,?)',
                 (generation, token, time.time() + LEASE_SECONDS, digest))
    counts = Counter(slot.surface for slot in plan.slots)
    files = Counter(output.surface for output in plan.outputs)
    conn.executemany('INSERT INTO recommendation_generation_surfaces VALUES (?,?,?,?)',
                     [(generation, surface, counts[surface], files[surface]) for surface in sorted(counts.keys() | files.keys())])
    return Build(generation, token, digest)


def claim(repo, plan, digest, *, owner=None):
    with proposal_cache._write(repo) as conn:
        previous = conn.execute('SELECT id,output_digest,status FROM recommendation_generations WHERE external_key=?',
                                (plan.external_key,)).fetchone()
        if previous:
            if previous[1] != digest:
                raise ValueError('Recommendation generation changed its immutable plan')
            if previous[2] in ('published', 'retiring'):
                raise WorkBudgetExceeded('Recommendation generation has already been published or retired')
            lease = conn.execute('SELECT owner,expires_at FROM recommendation_builds WHERE generation_id=?',
                                 (previous[0],)).fetchone()
            if lease is None or lease[0] == 'cleanup':
                raise WorkBudgetExceeded('Recommendation generation is awaiting cleanup')
            if owner != lease[0] and lease[1] > time.time():
                raise WorkBudgetExceeded('Recommendation generation is leased by another worker')
            token = owner if owner == lease[0] else uuid.uuid4().hex
            conn.execute('UPDATE recommendation_builds SET owner=?,expires_at=? WHERE generation_id=?',
                         (token, time.time() + LEASE_SECONDS, previous[0]))
            return Build(previous[0], token, digest)
        return _create(conn, plan, digest)


def _create_pool(conn, prepared):
    return conn.execute('INSERT INTO recommendation_pools(pool_key,policy_version,group_version_id,'
        'input_fingerprint,content_digest,purpose,eligible_count) VALUES (?,?,?,?,?,?,?) RETURNING id',
        (prepared.pool_key, prepared.policy_version, prepared.group_version_id, prepared.input_fingerprint,
         prepared.content_digest, prepared.purpose, len(prepared.candidates))).fetchone()[0]


def begin_pool(repo, build, prepared):
    with proposal_cache._write(repo) as conn:
        _touch(conn, build)
        row = conn.execute('SELECT id,complete FROM recommendation_pools WHERE content_digest=?',
                            (prepared.content_digest,)).fetchone()
        if row is None:
            identity = _create_pool(conn, prepared)
            conn.execute('INSERT INTO recommendation_build_pools VALUES (?,?)', (build.generation_id, identity))
            pools.append(conn, identity, prepared.candidates[:BATCH_ROWS], 0)
            count = min(BATCH_ROWS, len(prepared.candidates))
            sealed = False
        else:
            identity, sealed = row
            count = None
            if conn.execute('SELECT 1 FROM recommendation_pool_retirements WHERE pool_id=?', (identity,)).fetchone():
                raise WorkBudgetExceeded('Recommendation pool is awaiting bounded cleanup')
            owner = conn.execute('SELECT generation_id FROM recommendation_build_pools WHERE pool_id=?', (identity,)).fetchone()
            if not sealed and (owner is None or owner[0] != build.generation_id):
                raise WorkBudgetExceeded('Recommendation pool belongs to another unfinished build')
            if sealed:
                conn.execute('INSERT INTO recommendation_build_pools VALUES (?,?) ON CONFLICT DO NOTHING',
                             (build.generation_id, identity))
    return identity, bool(sealed), count


def _output(conn, plan, generation, output, file):
    pool_id = None
    if output.pool_digest is not None:
        pool_id = conn.execute('SELECT id FROM recommendation_pools WHERE content_digest=?',
                                (output.pool_digest,)).fetchone()[0]
    conn.execute('INSERT INTO prepared_outputs(generation_id,surface,chunk_key,filename,content_digest,'
        'byte_size,format_version,built_at,expires_at,pool_id,first_rank,last_rank) '
        'VALUES (?,?,?,?,?,?,1,?,?,?,?,?)',
        (generation, output.surface, output.chunk_key, *file, plan.created_at, output.expires_at,
         pool_id, output.first_rank, output.last_rank))


def _ready(conn, plan, generation):
    if plan.framing:
        existing = conn.execute('SELECT * FROM recommendation_bundle_framing WHERE generation_id=?',
                                (generation,)).fetchone()
        values = (generation, *astuple(plan.framing))
        if existing is None:
            conn.execute('INSERT INTO recommendation_bundle_framing VALUES (' + ','.join('?' for _ in values) + ')', values)
        elif tuple(existing) != values:
            raise ValueError('Recommendation staged bundle framing changed')
    status = conn.execute('SELECT status FROM recommendation_generations WHERE id=?', (generation,)).fetchone()[0]
    if status == 'building':
        conn.execute("UPDATE recommendation_generations SET status='ready' WHERE id=?", (generation,))


def finish(conn, build, at):
    """Switch the committed recommendation head in its repository's SQL write."""
    if not conn.in_transaction or not _finite(at):
        raise RuntimeError('Recommendation publication requires an outer transaction and finite time')
    _touch(conn, build)
    if conn.execute('SELECT 1 FROM recommendation_build_pools WHERE generation_id=?', (build.generation_id,)).fetchone():
        raise ValueError('Recommendation publication has unfinished pools')
    updated = conn.execute("UPDATE recommendation_generations SET status='published',published_at=? "
                           "WHERE id=? AND status='ready'", (at, build.generation_id)).rowcount
    if not updated:
        raise WorkBudgetExceeded('Recommendation generation is not ready to publish')
    conn.execute('INSERT INTO recommendation_heads(surface,generation_id) SELECT surface,generation_id '
                 'FROM recommendation_generation_surfaces WHERE generation_id=? '
                 'ON CONFLICT(surface) DO UPDATE SET generation_id=excluded.generation_id', (build.generation_id,))
    conn.execute('DELETE FROM recommendation_builds WHERE generation_id=?', (build.generation_id,))


def preflight(conn, plan):
    """Resolve the complete import before writing an owner or publishing a file."""
    digest = validate(plan)
    if conn.execute('SELECT 1 FROM recommendation_generations WHERE external_key=?', (plan.external_key,)).fetchone():
        raise ValueError('Recommendation import generation already exists')
    row = conn.execute('SELECT COALESCE(base_version,version) FROM recommendation_policies WHERE version=?',
                       (plan.policy_version,)).fetchone()
    if row is None:
        raise ValueError('Recommendation import requires its recorded policy')
    family = row[0]
    if plan.group_generation_id is not None and not conn.execute(
            'SELECT 1 FROM group_publications WHERE generation_id=?', (plan.group_generation_id,)).fetchone():
        raise ValueError('Recommendation import requires its published group generation')
    distinct = {slot.pool.content_digest: slot.pool for slot in plan.slots}
    for prepared in distinct.values():
        check_cancellable_work()
        row = conn.execute('SELECT COALESCE(base_version,version) FROM recommendation_policies WHERE version=?',
                           (prepared.policy_version,)).fetchone()
        if row is None or row[0] != family:
            raise ValueError('Recommendation import pool has incompatible policy inputs')
        if prepared.group_version_id is not None and not conn.execute(
                'SELECT 1 FROM generation_groups WHERE generation_id=? AND group_version_id=?',
                (plan.group_generation_id, prepared.group_version_id)).fetchone():
            raise ValueError('Recommendation import pool has incompatible group inputs')
        existing = conn.execute('SELECT id,complete FROM recommendation_pools WHERE content_digest=?',
                                (prepared.content_digest,)).fetchone()
        if existing:
            if not existing[1] or conn.execute('SELECT 1 FROM recommendation_pool_retirements WHERE pool_id=?',
                                              (existing[0],)).fetchone():
                raise ValueError('Recommendation import cannot reuse an unfinished or retiring pool')
            pools.verify(conn, existing[0], prepared)
        for start in range(0, len(prepared.candidates), BATCH_ROWS):
            identities = [candidate.recording_id for candidate in prepared.candidates[start:start + BATCH_ROWS]
                          if isinstance(candidate, pools.Candidate)]
            if identities:
                marks = ','.join('?' for _ in identities)
                found = {row[0] for row in conn.execute(f'SELECT id FROM recordings WHERE id IN ({marks})', identities)}
                if found != set(identities):
                    raise ValueError('Recommendation import has an unresolved canonical recording')
    return digest


def import_plan(conn, plan, published_at, *, before_publish=None):
    """Install one fully preflighted legacy generation in the migration transaction.

    The caller owns rollback and removal of its original source. This entry point
    does not acquire serving locks, commit intermediate prefixes or delete source
    evidence. Files left by a failed outer transaction are unowned and collectible.
    """
    if not conn.in_transaction or not _finite(published_at):
        raise RuntimeError('Recommendation import requires an outer transaction and finite time')
    digest = preflight(conn, plan)
    build = _create(conn, plan, digest)
    installed = {}
    for slot in plan.slots:
        check_cancellable_work()
        prepared = slot.pool
        if prepared.content_digest not in installed:
            row = conn.execute('SELECT id FROM recommendation_pools WHERE content_digest=?',
                               (prepared.content_digest,)).fetchone()
            identity = row[0] if row else _create_pool(conn, prepared)
            if row is None:
                for start in range(0, len(prepared.candidates), BATCH_ROWS):
                    check_cancellable_work()
                    pools.append(conn, identity, prepared.candidates[start:start + BATCH_ROWS], start)
                pools.verify(conn, identity, prepared)
                conn.execute('UPDATE recommendation_pools SET complete=1 WHERE id=?', (identity,))
            installed[prepared.content_digest] = identity
        conn.execute('INSERT INTO recommendation_surface_pools VALUES (?,?,?,?,?)',
                     (build.generation_id, slot.surface, slot.mode_key, slot.purpose, installed[prepared.content_digest]))
    root = outputs.directory(conn)
    for output in plan.outputs:
        check_cancellable_work()
        _output(conn, plan, build.generation_id, output, outputs.publish(root, output.encoded))
    _ready(conn, plan, build.generation_id)
    if before_publish is not None:
        before_publish(build.generation_id)
    finish(conn, build, published_at)
    return build.generation_id


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
    return conn.execute('SELECT g.id,g.status FROM recommendation_generations g WHERE NOT EXISTS('
        'SELECT 1 FROM recommendation_heads h WHERE h.generation_id=g.id) AND NOT EXISTS('
        'SELECT 1 FROM recommendation_generation_refs r WHERE r.generation_id=g.id) AND ('
        "g.status IN ('published','retiring') OR EXISTS(SELECT 1 FROM recommendation_builds b "
        'WHERE b.generation_id=g.id AND b.expires_at<?)) ORDER BY g.id LIMIT 1', (time.time(),)).fetchone()


def _orphan_pools(conn):
    revalidations = ('AND NOT EXISTS(SELECT 1 FROM recommendation_pool_revalidations v WHERE v.pool_id=p.id) '
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_pool_revalidations'").fetchone() else '')
    return ('SELECT p.id FROM recommendation_pools p WHERE NOT EXISTS('
        'SELECT 1 FROM recommendation_surface_pools m WHERE m.pool_id=p.id) AND NOT EXISTS('
        'SELECT 1 FROM recommendation_build_pools b WHERE b.pool_id=p.id) AND NOT EXISTS('
        'SELECT 1 FROM prepared_outputs o WHERE o.pool_id=p.id) ' + revalidations + 'ORDER BY p.id LIMIT ?')


def retirement_pending(conn):
    """Use the collectors' actual owners, including pools detached from a deleted generation."""
    return bool(_retiring_generation(conn) or conn.execute(_orphan_pools(conn), (1,)).fetchone())


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
                conn.execute("UPDATE recommendation_generations SET status='retiring' WHERE id=?", (identity,))
            conn.execute("UPDATE recommendation_builds SET owner='cleanup',expires_at=0 WHERE generation_id=?", (identity,))
            revalidations = (('recommendation_pool_revalidations', ('generation_id', 'surface', 'mode_key', 'purpose')),)
            if not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_pool_revalidations'").fetchone():
                revalidations = ()
            for table, columns in (('prepared_outputs', ('id',)),
                                   ('recommendation_bundle_framing', ('generation_id',)),
                                   ('recommendation_surface_pools', ('generation_id', 'surface', 'mode_key', 'purpose')),
                                   *revalidations,
                                   ('recommendation_build_pools', ('generation_id', 'pool_id')),
                                   ('recommendation_generation_surfaces', ('generation_id', 'surface'))):
                removed += _delete(conn, table, columns, 'generation_id=?', (identity,), row_limit - removed)
                if removed == row_limit:
                    return Retirement(rows=removed)
            conn.execute('DELETE FROM recommendation_builds WHERE generation_id=?', (identity,))
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
        selected = tuple(row[0] for row in conn.execute(_orphan_pools(conn), (pool_limit,)))
        if not selected:
            return Retirement()
        marks = ','.join('?' for _ in selected)
        conn.execute('INSERT INTO recommendation_pool_retirements '
            'SELECT id FROM recommendation_pools WHERE id IN (' + marks + ') ON CONFLICT DO NOTHING', selected)
        for table, key in (('recommendation_radio_bands', 'recording_id'),
                           ('recommendation_candidates', 'recording_id'), ('unresolved_recommendation_candidates', 'raw_key')):
            check_cancellable_work()
            removed += _delete(conn, table, ('pool_id', key), 'pool_id IN (' + marks + ')', selected, row_limit - removed)
            if removed == row_limit:
                break
        # Delete empty parents together, even when another selected pool still
        # has children to drain next time. The reservation and retirement fence
        # keep the owner decision valid through this commit.
        empty = tuple(row[0] for row in conn.execute(
            'SELECT p.id FROM recommendation_pools p WHERE p.id IN (' + marks + ') AND '
            'NOT EXISTS(SELECT 1 FROM recommendation_candidates c WHERE c.pool_id=p.id) AND '
            'NOT EXISTS(SELECT 1 FROM recommendation_radio_bands b WHERE b.pool_id=p.id) AND '
            'NOT EXISTS(SELECT 1 FROM unresolved_recommendation_candidates c WHERE c.pool_id=p.id)', selected))
        if empty:
            marks = ','.join('?' for _ in empty)
            conn.execute('DELETE FROM recommendation_pool_retirements WHERE pool_id IN (' + marks + ')', empty)
            retired = conn.execute('DELETE FROM recommendation_pools WHERE id IN (' + marks + ')', empty).rowcount
    return Retirement(rows=removed, pools=retired)
