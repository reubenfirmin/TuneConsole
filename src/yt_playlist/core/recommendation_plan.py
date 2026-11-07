"""Immutable recommendation publication plans and admission rules."""
from dataclasses import dataclass, astuple
import hashlib
import math

from . import recommendation_outputs as outputs, recommendation_pools as pools
from .evidence_ledger import canonical

MAX_PLAN_BYTES = 128 * 1024 * 1024
MAX_SLOTS = 16384
MAX_SURFACES = 128
BATCH_ROWS = 128
SLOT_BATCH_ROWS = 16
OUTPUT_BATCH_ROWS = 16


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


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(plan, *, allow_unobserved=False):
    """Admit all logical output before creating rows or files."""
    if (not isinstance(plan, Plan) or not _identity(plan.external_key) or not _identity(plan.policy_version)
            or plan.group_generation_id is not None and
            (type(plan.group_generation_id) is not int or plan.group_generation_id < 1)
            or plan.input_revision is not None and (type(plan.input_revision) is not int or plan.input_revision < 0)
            or not (finite(plan.created_at) or allow_unobserved and plan.created_at is None)
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
                or output.expires_at is not None and not finite(output.expires_at)):
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
    if len(surfaces) > MAX_SURFACES:
        raise ValueError('Recommendation publication exceeds its surface-header budget')
    return digest.hexdigest()
