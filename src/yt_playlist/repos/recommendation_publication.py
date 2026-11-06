"""Leased bounded preparation and one guarded recommendation head switch."""
from collections import Counter
from dataclasses import dataclass, astuple
import hashlib
import math
import time
import uuid

from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools, policy_documents
from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.recommendation_custody import retain, release
from . import policy_storage, proposal_cache

BATCH_ROWS = 128
SLOT_BATCH_ROWS = 16
OUTPUT_BATCH_ROWS = 16
CLEANUP_ROWS = 1024
CLEANUP_POOLS = 32
LEASE_SECONDS = 60
MAX_PLAN_BYTES = 128 * 1024 * 1024
MAX_SLOTS = 16384


@dataclass(frozen=True)
class Slot:
    surface: str
    mode_key: str
    purpose: str
    pool: pools.Pool
    revalidated_policy: str | None = None

    @property
    def revalidated(self):
        return self.revalidated_policy is not None


@dataclass(frozen=True)
class Output:
    surface: str
    chunk_key: str
    encoded: bytes
    expires_at: float | None = None
    pool_digest: str | None = None
    first_rank: int | None = None
    last_rank: int | None = None


@dataclass(frozen=True)
class Framing:
    presence: int = 0
    comfort_pool: int | None = None
    year_low: int | None = None
    year_high: int | None = None
    full_pools: bool | None = None
    unmodeled: bool | None = None
    musical_generation: str | None = None
    radio_generation: str | None = None
    radio_space: str | None = None
    intensity_version: int | None = None
    radio_profile_version: str | None = None
    vibe_version: str | None = None
    vibe_profile_version: str | None = None


@dataclass(frozen=True)
class Plan:
    external_key: str
    policy_version: str
    group_generation_id: int | None
    input_revision: int | None
    created_at: float | None
    slots: tuple[Slot, ...] = ()
    outputs: tuple[Output, ...] = ()
    framing: Framing | None = None


@dataclass(frozen=True)
class Build:
    generation_id: int
    owner: str
    plan_digest: str


def _identity(value, *, empty=False):
    return isinstance(value, str) and (empty or bool(value)) and len(value.encode()) <= 1024


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(plan, *, allow_unobserved=False):
    """Admit all logical output before creating rows or files."""
    if (not isinstance(plan, Plan) or not _identity(plan.external_key) or not _identity(plan.policy_version)
            or plan.group_generation_id is not None and
            (type(plan.group_generation_id) is not int or plan.group_generation_id < 1)
            or plan.input_revision is not None and (type(plan.input_revision) is not int or plan.input_revision < 0)
            or not (_finite(plan.created_at) or allow_unobserved and plan.created_at is None)
            or not isinstance(plan.slots, tuple) or not isinstance(plan.outputs, tuple)
            or len(plan.slots) + len(plan.outputs) > MAX_SLOTS or not (plan.slots or plan.outputs)):
        raise ValueError('Invalid recommendation publication plan')
    if plan.framing is not None:
        framing = plan.framing
        if (not isinstance(framing, Framing) or type(framing.presence) is not int or not 0 <= framing.presence <= 2047
                or any(value is not None and (type(value) is not int or value < 0)
                       for value in (framing.comfort_pool, framing.intensity_version))
                or any(value is not None and type(value) is not int for value in (framing.year_low, framing.year_high))
                or (framing.year_low is None) != (framing.year_high is None)
                or framing.year_low is not None and framing.year_low > framing.year_high
                or any(value is not None and type(value) is not bool for value in (framing.full_pools, framing.unmodeled))
                or any(value is not None and not _identity(value, empty=True) for value in (framing.musical_generation,
                    framing.radio_generation, framing.radio_space, framing.radio_profile_version,
                    framing.vibe_version, framing.vibe_profile_version))):
            raise ValueError('Invalid recommendation bundle framing')
    digest = hashlib.sha256(canonical([plan.external_key, plan.policy_version, plan.group_generation_id,
                                      plan.input_revision, plan.created_at, astuple(plan.framing) if plan.framing else None]))
    used, slots, files, distinct, surfaces, selections, ranges = 0, set(), set(), {}, set(), set(), {}
    for slot in plan.slots:
        if (not isinstance(slot, Slot) or not _identity(slot.surface) or not _identity(slot.mode_key, empty=True)
                or not _identity(slot.purpose) or not isinstance(slot.pool, pools.Pool)
                or slot.revalidated and not _identity(slot.revalidated_policy)):
            raise ValueError('Invalid recommendation pool slot')
        key = slot.surface, slot.mode_key, slot.purpose
        if key in slots:
            raise ValueError('Duplicate recommendation pool slot')
        slots.add(key)
        surfaces.add(slot.surface)
        pool = slot.pool
        selections.add((slot.surface, pool.content_digest))
        if pool.content_digest not in distinct:
            actual = pools.prepare(pool.pool_key, pool.policy_version, pool.group_version_id,
                                   pool.input_fingerprint, pool.purpose, pool.candidates)
            if actual != pool:
                raise ValueError('Recommendation pool content identity changed')
            used += sum(len(row) for row in pools.rows(pool))
            distinct[pool.content_digest] = pool
        elif distinct[pool.content_digest] != pool:
            raise ValueError('Conflicting recommendation pool content identity')
        digest.update(canonical([*key, pool.content_digest, *([slot.revalidated_policy] if slot.revalidated else [])]))
        if used > MAX_PLAN_BYTES:
            raise ValueError('Recommendation publication exceeds its admitted memory')
    for output in plan.outputs:
        if (not isinstance(output, Output) or not _identity(output.surface)
                or not _identity(output.chunk_key, empty=True) or not isinstance(output.encoded, bytes)
                or len(output.encoded) > outputs.MAX_BYTES
                or output.expires_at is not None and not _finite(output.expires_at)):
            raise ValueError('Invalid recommendation output chunk')
        key = output.surface, output.chunk_key
        if key in files:
            raise ValueError('Duplicate recommendation output chunk')
        files.add(key)
        surfaces.add(output.surface)
        document = outputs._decode(output.encoded, *key)
        if output.pool_digest is None:
            if output.first_rank is not None or output.last_rank is not None:
                raise ValueError('Display ranks require a selected pool')
        else:
            if (not isinstance(output.pool_digest, str) or (output.surface, output.pool_digest) not in selections
                    or type(output.first_rank) is not int or type(output.last_rank) is not int
                    or not 0 <= output.first_rank <= output.last_rank < len(distinct[output.pool_digest].candidates)):
                raise ValueError('Display ranks require a selected pool')
            value = document['output']
            if not isinstance(value, dict) or set(value) != {'rows'} or not isinstance(value['rows'], list) or not value['rows']:
                raise ValueError('Invalid ranked display output')
            previous = output.first_rank - 1
            for row in value['rows']:
                if (not isinstance(row, list) or len(row) != 3 or type(row[0]) is not int
                        or not previous < row[0] <= output.last_rank or not isinstance(row[2], dict)
                        or row[1] != pools.owner(distinct[output.pool_digest].candidates[row[0]])):
                    raise ValueError('Display output differs from its ranked pool')
                previous = row[0]
            if value['rows'][0][0] != output.first_rank or previous != output.last_rank:
                raise ValueError('Display output differs from its rank range')
            ranges.setdefault((output.surface, output.pool_digest), []).append((output.first_rank, output.last_rank))
        used += len(output.encoded)
        if used > MAX_PLAN_BYTES:
            raise ValueError('Recommendation publication exceeds its admitted memory')
        digest.update(canonical([*key, hashlib.sha256(output.encoded).hexdigest(), output.expires_at,
                                 output.pool_digest, output.first_rank, output.last_rank]))
    for intervals in ranges.values():
        intervals.sort()
        if any(left[1] >= right[0] for left, right in zip(intervals, intervals[1:])):
            raise ValueError('Overlapping ranked display output')
    if len(surfaces) > BATCH_ROWS:
        raise ValueError('Recommendation publication exceeds its surface-header budget')
    return digest.hexdigest()


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


def claim(repo, plan, *, owner=None, policies=(), bases=()):
    digest = validate(plan)
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
            if policies or bases:
                policy_storage.publish(conn, policies, bases, enforce_budget=True)
            token = owner if owner == lease[0] else uuid.uuid4().hex
            conn.execute('UPDATE recommendation_builds SET owner=?,expires_at=? WHERE generation_id=?',
                         (token, time.time() + LEASE_SECONDS, previous[0]))
            return Build(previous[0], token, digest)
        if policies or bases:
            policy_storage.publish(conn, policies, bases, enforce_budget=True)
        return _create(conn, plan, digest)


def _create_pool(conn, prepared):
    return conn.execute('INSERT INTO recommendation_pools(pool_key,policy_version,group_version_id,'
        'input_fingerprint,content_digest,purpose,eligible_count) VALUES (?,?,?,?,?,?,?) RETURNING id',
        (prepared.pool_key, prepared.policy_version, prepared.group_version_id, prepared.input_fingerprint,
         prepared.content_digest, prepared.purpose, len(prepared.candidates))).fetchone()[0]


def _pool(repo, build, prepared, *, still_current=None):
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
    if not sealed:
        if count is None:
            with repo._store.read_view() as reader:
                count = reader.conn.execute('SELECT (SELECT count(*) FROM recommendation_candidates WHERE pool_id=?)'
                    '+(SELECT count(*) FROM unresolved_recommendation_candidates WHERE pool_id=?)', (identity, identity)).fetchone()[0]
        if count > len(prepared.candidates):
            raise ValueError('Recommendation candidate prefix exceeds its plan')
        for start in range(count, len(prepared.candidates), BATCH_ROWS):
            if still_current is not None and not still_current():
                return None
            with proposal_cache._write(repo) as conn:
                _touch(conn, build)
                pools.append(conn, identity, prepared.candidates[start:start + BATCH_ROWS], start)
    with repo._store.read_view() as reader:
        pools.verify(reader.conn, identity, prepared)
    return identity


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


def stage(repo, plan, build, *, still_current=None):
    if validate(plan) != build.plan_digest:
        raise ValueError('Recommendation staged plan changed')
    for start in range(0, len(plan.slots), SLOT_BATCH_ROWS):
        if still_current is not None and not still_current():
            return False
        pending = []
        with repo._store.read_view() as reader:
            # Share only the bounded selection lookup. Each pool still gets its
            # own complete off-lock verification and bounded write transactions.
            for slot in plan.slots[start:start + SLOT_BATCH_ROWS]:
                check_cancellable_work()
                existing = reader.conn.execute('SELECT p.content_digest FROM recommendation_surface_pools m '
                    'JOIN recommendation_pools p ON p.id=m.pool_id WHERE m.generation_id=? AND m.surface=? '
                    'AND m.mode_key=? AND m.purpose=?',
                    (build.generation_id, slot.surface, slot.mode_key, slot.purpose)).fetchone()
                if existing:
                    if existing[0] != slot.pool.content_digest:
                        raise ValueError('Recommendation staged pool selection changed')
                else:
                    pending.append(slot)
        for slot in pending:
            check_cancellable_work()
            identity = (_pool(repo, build, slot.pool, still_current=still_current)
                        if still_current is not None else _pool(repo, build, slot.pool))
            if identity is None:
                return False
            with proposal_cache._write(repo) as conn:
                _touch(conn, build)
                # Verification stays off-lock; sealing and attachment share a commit.
                conn.execute('UPDATE recommendation_pools SET complete=1 WHERE id=? AND complete=0', (identity,))
                if slot.revalidated:
                    # _pool verified every canonical fact outside this lock.
                    # This immutable owner explicitly records rechecking those
                    # facts under the new generation's captured input policy.
                    conn.execute('INSERT INTO recommendation_pool_revalidations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                                 (build.generation_id, slot.surface, slot.mode_key, slot.purpose,
                                  identity, slot.revalidated_policy))
                conn.execute('INSERT INTO recommendation_surface_pools VALUES (?,?,?,?,?)',
                             (build.generation_id, slot.surface, slot.mode_key, slot.purpose, identity))
                conn.execute('DELETE FROM recommendation_build_pools WHERE generation_id=? AND pool_id=?',
                             (build.generation_id, identity))
    root = outputs.root_for_database(repo._store._db_path, repo._store._evidence_key)
    for start in range(0, len(plan.outputs), OUTPUT_BATCH_ROWS):
        if still_current is not None and not still_current():
            return False
        pending = []
        with repo._store.read_view() as reader:
            for output in plan.outputs[start:start + OUTPUT_BATCH_ROWS]:
                check_cancellable_work()
                existing = reader.conn.execute('SELECT content_digest FROM prepared_outputs WHERE generation_id=? '
                    'AND surface=? AND chunk_key=?', (build.generation_id, output.surface, output.chunk_key)).fetchone()
                if existing:
                    if existing[0] != hashlib.sha256(output.encoded).hexdigest():
                        raise ValueError('Recommendation staged output changed')
                else:
                    pending.append(output)
        if not pending:
            continue
        # The shared lifetime lock fences file GC while bytes are durable but
        # their owner rows are not committed. Only the bounded manifest batch
        # takes the serving lock; every file is synced before its SQL owner.
        with policy_documents.retained_reader(root):
            files = []
            for output in pending:
                check_cancellable_work()
                files.append((output, outputs.publish(root, output.encoded)))
            with proposal_cache._write(repo) as conn:
                _touch(conn, build)
                for output, file in files:
                    check_cancellable_work()
                    _output(conn, plan, build.generation_id, output, file)
    if still_current is not None and not still_current():
        return False
    with proposal_cache._write(repo) as conn:
        _touch(conn, build)
        _ready(conn, plan, build.generation_id)
    return True


def finish(conn, build, at):
    """Join the snapshot publisher's transaction; policies/settings commit here too."""
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


def cleanup_generations(repo, *, checkpoint=True):
    """Fence a no-longer-required generation, then remove a bounded prefix."""
    with proposal_cache._write(repo, checkpoint=checkpoint) as conn:
        row = conn.execute('SELECT g.id,g.status FROM recommendation_generations g WHERE NOT EXISTS('
            'SELECT 1 FROM recommendation_heads h WHERE h.generation_id=g.id) AND NOT EXISTS('
            'SELECT 1 FROM recommendation_generation_refs r WHERE r.generation_id=g.id) AND ('
            "g.status IN ('published','retiring') OR EXISTS(SELECT 1 FROM recommendation_builds b "
            'WHERE b.generation_id=g.id AND b.expires_at<?)) ORDER BY g.id LIMIT 1', (time.time(),)).fetchone()
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
                removed += _delete(conn, table, columns, 'generation_id=?', (identity,), CLEANUP_ROWS - removed)
                if removed == CLEANUP_ROWS:
                    return Retirement(rows=removed)
            conn.execute('DELETE FROM recommendation_builds WHERE generation_id=?', (identity,))
            conn.execute('DELETE FROM recommendation_generations WHERE id=?', (identity,))
            return Retirement(rows=removed, generations=1)
        return Retirement()


def cleanup_pools(repo, *, checkpoint=True):
    """Drain orphan pools even while generations remain eligible for retirement."""
    removed = retired = 0
    with proposal_cache._write(repo, checkpoint=checkpoint) as conn:
        # The writer reservation keeps this bounded selection valid throughout
        # the transaction. Repeating the same anti-joins for each small pool
        # can consume the whole turn once retained pools lead the ID order.
        revalidations = ('AND NOT EXISTS(SELECT 1 FROM recommendation_pool_revalidations v WHERE v.pool_id=p.id) '
            if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_pool_revalidations'").fetchone() else '')
        selected = conn.execute('SELECT p.id FROM recommendation_pools p WHERE NOT EXISTS('
            'SELECT 1 FROM recommendation_surface_pools m WHERE m.pool_id=p.id) AND NOT EXISTS('
            'SELECT 1 FROM recommendation_build_pools b WHERE b.pool_id=p.id) AND NOT EXISTS('
            'SELECT 1 FROM prepared_outputs o WHERE o.pool_id=p.id) ' + revalidations + 'ORDER BY p.id LIMIT ?',
            (CLEANUP_POOLS,)).fetchall()
        for pool in selected:
            check_cancellable_work()
            identity = pool[0]
            conn.execute('INSERT INTO recommendation_pool_retirements VALUES (?) ON CONFLICT DO NOTHING', (identity,))
            for table, key in (('recommendation_radio_bands', 'recording_id'),
                               ('recommendation_candidates', 'recording_id'), ('unresolved_recommendation_candidates', 'raw_key')):
                removed += _delete(conn, table, ('pool_id', key), 'pool_id=?', (identity,), CLEANUP_ROWS - removed)
                if removed == CLEANUP_ROWS:
                    return Retirement(rows=removed, pools=retired)
            conn.execute('DELETE FROM recommendation_pool_retirements WHERE pool_id=?', (identity,))
            conn.execute('DELETE FROM recommendation_pools WHERE id=?', (identity,))
            retired += 1
    return Retirement(rows=removed, pools=retired)
