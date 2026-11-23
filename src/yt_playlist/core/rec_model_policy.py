"""Taste-weight behavior and embedding deltas over detached model facts."""
from dataclasses import dataclass
import json

from . import embeddings

WEIGHT_MIN, WEIGHT_MAX = .2, 3.
SESSION_WEIGHTS = 'taste_session_weights_v1'
SESSION_IDLE_S = 30 * 60
PERMANENT_SURFACE = 'listener_permanent_v2'
PERMANENT_WORK = 'listener_permanent_work_v2'
STARTUP_WEIGHT_POLICY = 'exclude-legacy-dislikes'
CHOICE_SUPPORT_SURFACE = 'listener_choice_support_v2'


def session_identity(session):
    return [session.account, session.started_at] if session is not None else None


def steering_remaining(at, session_id, session, now, first_session):
    if now < at:
        return 0.
    if session_id is None:
        until = min(now, at + SESSION_IDLE_S)
        session_id = session_identity(first_session(at, until))
    if session_id is not None:
        return max(0., session.expires_at - now) if session_identity(session) == session_id else 0.
    return max(0., at + SESSION_IDLE_S - now)


def session_weights(raw, session, now, first_session):
    if not raw:
        return {}
    document = json.loads(raw)
    return {axis: row['value'] for axis, row in document.items()
            if steering_remaining(row['at'], row['session'], session, now, first_session) > 0}


@dataclass(frozen=True)
class VectorChanges:
    upserts: tuple[tuple[str, bytes], ...]
    removed: tuple[str, ...]
    replace: bool = False
    model_version: str | None = None
    publication: embeddings.Publication | None = None


def reverted(weight, updated_at, now, halflife_d):
    if updated_at is None or now is None:
        return weight
    age = max(0., (now - updated_at) / 86400.)
    return 1. + (weight - 1.) * .5 ** (age / float(halflife_d))


def effective(axis, row, now, halflife_d):
    if axis.startswith('genre:') and row['weight'] == 0.:
        return 0.
    return reverted(row['weight'], row['updated_at'], now, halflife_d)


def startup_weight(events, halflife_d):
    """Reconstruct a legacy graduation weight without its dislike multipliers."""
    # The legacy graduate path capped weights at 2. Dislikes only lowered them,
    # so a recorded weight at the cap has already absorbed any earlier penalty.
    cap = 2.
    preceding, preceding_at, adjustment = 1., None, 0.
    for source, factor, recorded, at in events:
        if recorded >= cap or recorded <= 0. or factor <= 0.:
            adjustment = 0.
        else:
            before = recorded / factor
            if adjustment:
                # The old reversion policy could change between writes. Use the
                # recorded movement toward 1 to recover the actual decay.
                if preceding == 1.:
                    retention = 0.
                    if abs(before - 1.) < 1e-12:
                        retention = reverted(2., preceding_at, at, halflife_d) - 1.
                else:
                    retention = (before - 1.) / (preceding - 1.)
                # A jump incompatible with decay starts a new sequence.
                adjustment *= min(1., max(0., retention)) if -1e-12 <= retention <= 1. + 1e-12 else 0.
            if source != 'dislike':
                adjustment *= factor
            else:
                adjustment += before - recorded
            adjustment = min(cap - recorded, max(0., adjustment))
        preceding, preceding_at = recorded, at
    return preceding + adjustment


def training_delta(table, rows, current, head, settings):
    rows, current = tuple(rows), dict(current)
    version = embeddings.version(table, settings)
    if len({key for key, _ in rows}) != len(rows):
        raise ValueError('Duplicate training vector key')
    dimension = embeddings.model_dimensions(table, settings)
    if dimension is not None and any(embeddings.dimensions(blob) != dimension for _key, blob in rows):
        raise ValueError('Embedding dimensions do not match their encoder')
    if version and head and version != head[0]:
        return VectorChanges(rows, (), replace=True, model_version=version)
    old_order = tuple(current) if table == 'rec_discovered_content_vectors' else None
    upserts, seen = [], set()
    for key, blob in rows:
        seen.add(key)
        if current.pop(key, None) != blob:
            upserts.append((key, blob))
    if old_order is not None:
        old_keys = set(old_order)
        order = [key for key in old_order if key in seen]
        order.extend(key for key, _ in rows if key not in old_keys)
        if order != [key for key, _ in rows]:
            return VectorChanges(rows, (), replace=True, model_version=version)
    if upserts or current:
        return VectorChanges(tuple(upserts), tuple(current), model_version=version if head is None else None)
