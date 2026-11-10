"""Complete ranked membership with explicit numerical filtering dependencies.

Pool content contains canonical recording IDs, ranks and scores. Display fields
and musical measurements remain with their catalog and recording-vibe owners.
"""
from dataclasses import dataclass
from itertools import chain
import math

from . import group_content
from .evidence_ledger import canonical

FORMAT = 'recommendation-pool-1'
MAX_POOL_BYTES = 32 * 1024 * 1024
MAX_CANDIDATES = 250000
PURPOSES = frozenset({'card', 'radio', 'ppr', 'vibe', 'temporal', 'fresh'})


@dataclass(frozen=True)
class Candidate:
    recording_id: int
    score: float | None = None
    intensity_band: int | None = None
    genre_key: str | None = None


@dataclass(frozen=True)
class UnresolvedCandidate:
    raw_key: str
    score: float | None = None
    intensity_band: int | None = None
    genre_key: str | None = None


@dataclass(frozen=True)
class Pool:
    pool_key: str
    policy_version: str
    group_version_id: int | None
    input_fingerprint: str
    purpose: str
    candidates: tuple[Candidate | UnresolvedCandidate, ...]
    content_digest: str


def header(pool):
    return _header(pool, len(pool.candidates))


def _header(pool, count):
    return canonical([FORMAT, pool.pool_key, pool.policy_version, pool.group_version_id,
                      pool.input_fingerprint, pool.purpose, count])


def rows(pool):
    yield header(pool)
    for rank, candidate in enumerate(pool.candidates):
        yield canonical([owner(candidate), rank, candidate.score, candidate.intensity_band, candidate.genre_key])


def owner(candidate):
    return ['unresolved', candidate.raw_key] if isinstance(candidate, UnresolvedCandidate) else ['recording', candidate.recording_id]


def prepare(pool_key, policy_version, group_version_id, input_fingerprint, purpose, candidates):
    return _prepare(pool_key, policy_version, group_version_id, input_fingerprint, purpose, candidates)[0]


def validate(pool):
    """Verify the complete content and return its encoded size for plan admission."""
    actual, size = _prepare(pool.pool_key, pool.policy_version, pool.group_version_id,
                            pool.input_fingerprint, pool.purpose, pool.candidates)
    if actual != pool:
        raise ValueError('Recommendation pool content identity changed')
    return size


def _prepare(pool_key, policy_version, group_version_id, input_fingerprint, purpose, candidates):
    if (not isinstance(pool_key, str) or not pool_key or len(pool_key.encode()) > 1024
            or not isinstance(policy_version, str) or not policy_version or len(policy_version.encode()) > 1024
            or group_version_id is not None and (type(group_version_id) is not int or group_version_id < 1)
            or not isinstance(input_fingerprint, str) or len(input_fingerprint) != 64
            or any(c not in '0123456789abcdef' for c in input_fingerprint) or purpose not in PURPOSES):
        raise ValueError('Invalid recommendation pool inputs')
    normalized, identities, used = [], set(), 0

    def admitted_rows():
        nonlocal used
        for candidate in candidates:
            if not isinstance(candidate, (Candidate, UnresolvedCandidate)):
                raise ValueError('Recommendation candidates require typed canonical owners')
            kind, identity = owner(candidate)
            score, band, genre = candidate.score, candidate.intensity_band, candidate.genre_key
            if (kind == 'recording' and (type(identity) is not int or identity < 1)
                    or kind == 'unresolved' and (not isinstance(identity, str) or not identity or len(identity.encode()) > 1024)
                    or (kind, identity) in identities
                    or score is not None and (type(score) not in (int, float) or not math.isfinite(score))
                    or band is not None and (purpose != 'radio' or type(band) is not int or not 0 <= band <= 50)
                    or (band is None) != (genre is None)
                    or genre is not None and (not isinstance(genre, str) or len(genre.encode()) > 1024)):
                raise ValueError('Invalid or duplicate recommendation candidate')
            score = (float(score) if score else 0.0) if score is not None else None
            normalized.append(type(candidate)(identity, score, band, genre))
            identities.add((kind, identity))
            row = canonical([[kind, identity], len(normalized) - 1, score, band, genre])
            used += len(row)
            if used > MAX_POOL_BYTES or len(normalized) > MAX_CANDIDATES:
                raise ValueError('Recommendation pool exceeds its admitted membership budget')
            yield row

    value = Pool(pool_key, policy_version, group_version_id, input_fingerprint, purpose, (), '')
    if type(candidates) in (tuple, list):
        # The count is already known: admission and hashing stream the same
        # bytes once, retaining neither encoded rows nor a second encoded pool.
        count = len(candidates)
        encoded_header = _header(value, count)
        digest = group_content.digest(chain((encoded_header,), admitted_rows()))
        if len(normalized) != count:
            raise ValueError('Recommendation candidate input changed during admission')
    else:
        # Unknown-length iterables still stop on the first excess row. Do not
        # exhaust or materialize an unbounded input just to learn its count.
        for _ in admitted_rows():
            pass
        value = Pool(pool_key, policy_version, group_version_id, input_fingerprint, purpose, tuple(normalized), '')
        encoded_header = header(value)
        digest = group_content.digest(rows(value))
    prepared = Pool(pool_key, policy_version, group_version_id, input_fingerprint, purpose,
                    tuple(normalized), digest)
    return prepared, used + len(encoded_header)


def stored_rows(conn, pool_id):
    pool = conn.execute('SELECT pool_key,policy_version,group_version_id,input_fingerprint,purpose,eligible_count '
                        'FROM recommendation_pools WHERE id=?', (pool_id,)).fetchone()
    if pool is None:
        raise ValueError('Missing recommendation pool')
    yield canonical([FORMAT, *pool])
    for kind, recording, rank, score, band, genre in conn.execute(
            "SELECT 'recording' kind,c.recording_id owner,c.rank,c.score,b.intensity_band,b.genre_key FROM recommendation_candidates c "
            'LEFT JOIN recommendation_radio_bands b ON b.pool_id=c.pool_id AND b.recording_id=c.recording_id '
            "WHERE c.pool_id=? UNION ALL SELECT 'unresolved',raw_key,rank,score,intensity_band,genre_key "
            'FROM unresolved_recommendation_candidates WHERE pool_id=? ORDER BY rank', (pool_id, pool_id)):
        score = (float(score) if score else 0.0) if score is not None else None
        yield canonical([[kind, recording], rank, score, band, genre])


def verify(conn, pool_id, prepared):
    if not group_content.equal(stored_rows(conn, pool_id), rows(prepared)):
        raise ValueError('Recommendation pool digest collision or incomplete membership')


def append(conn, pool_id, candidates, start):
    if not conn.in_transaction:
        raise RuntimeError('Recommendation pool writes require an outer transaction')
    for rank, candidate in enumerate(candidates, start):
        if isinstance(candidate, UnresolvedCandidate):
            conn.execute("INSERT INTO unresolved_recommendation_candidates VALUES (?,?,?,?,?,?,'missing_recording')",
                (pool_id, candidate.raw_key, rank, candidate.score, candidate.intensity_band, candidate.genre_key))
            continue
        conn.execute('INSERT INTO recommendation_candidates VALUES (?,?,?,?)',
                     (pool_id, candidate.recording_id, rank, candidate.score))
        if candidate.intensity_band is not None:
            conn.execute('INSERT INTO recommendation_radio_bands VALUES (?,?,?,?)',
                         (pool_id, candidate.recording_id, candidate.intensity_band, candidate.genre_key))


def page(conn, pool_id, *, after=-1, limit=128, band=None, genres=None, group_version=None):
    """Keyset traversal retains complete membership without a global cutoff."""
    if type(after) is not int or after < -1 or type(limit) is not int or not 1 <= limit <= 512:
        raise ValueError('Invalid bounded recommendation page')
    if group_version is not None:
        if not isinstance(group_version, str) or not group_version or band is not None or genres is not None:
            raise ValueError('Invalid bounded group selection')
        # Start from the selected immutable member set, then seek each member in
        # the pool's composite owner index. A rare group never scans a global head.
        return [dict(row) for row in conn.execute(
            'SELECT c.recording_id,c.rank,c.score,NULL AS intensity_band,NULL AS genre_key '
            'FROM group_version_aliases a JOIN group_members m ON m.group_version_id=a.group_version_id '
            'CROSS JOIN recommendation_candidates c WHERE a.external_version=? '
            'AND c.pool_id=? AND c.recording_id=m.recording_id AND c.rank>? ORDER BY c.rank LIMIT ?',
            (group_version, pool_id, after, limit))]
    parameters = [pool_id, after]
    predicate = ''
    if band is not None:
        if type(band) is not int or not 0 <= band <= 50:
            raise ValueError('Invalid radio intensity band')
        predicate += ' AND b.intensity_band=?'
        parameters.append(band)
    if genres is not None:
        if not isinstance(genres, (list, tuple)) or len(genres) > 128 or any(not isinstance(g, str) for g in genres):
            raise ValueError('Invalid bounded radio genre selection')
        if not genres:
            return []
        predicate += ' AND b.genre_key IN (' + ','.join('?' for _ in genres) + ')'
        parameters.extend(genres)
    parameters.append(limit)
    if band is not None or genres is not None:
        # Start with the selected region. Scanning the ranked global head for a
        # rare band/genre defeats the purpose of retaining indexed membership.
        index = 'ix_recommendation_radio_band' if band is not None else 'ix_recommendation_radio_genre'
        return [dict(row) for row in conn.execute(
            'SELECT c.recording_id,c.rank,c.score,b.intensity_band,b.genre_key '
            f'FROM recommendation_radio_bands b INDEXED BY {index} '
            'CROSS JOIN recommendation_candidates c '
            'WHERE b.pool_id=? AND c.pool_id=b.pool_id AND c.recording_id=b.recording_id '
            'AND c.rank>?' + predicate + ' ORDER BY c.rank LIMIT ?', parameters)]
    return [dict(row) for row in conn.execute(
        'SELECT c.recording_id,c.rank,c.score,b.intensity_band,b.genre_key FROM recommendation_candidates c '
        'LEFT JOIN recommendation_radio_bands b ON b.pool_id=c.pool_id AND b.recording_id=c.recording_id '
        'WHERE c.pool_id=? AND c.rank>?' + predicate + ' ORDER BY c.rank LIMIT ?', parameters)]
