"""Explicit group roots owned by actual offers, responses and prepared contexts.

These rows retain exact external aliases. Resolving two aliases to the same
content never proves that either alias occurred in a particular served context.
Callers publish roots before acknowledging the corresponding external record.
"""
import hashlib
import math
from dataclasses import dataclass

from .evidence_ledger import canonical
from . import group_storage

KINDS = frozenset({'offer', 'response', 'comparison', 'prepared', 'pending', 'checkpoint'})


def references(context):
    if not isinstance(context, dict):
        raise ValueError('Musical group evidence context must be an object')
    generation = context.get('group_generation')
    if generation is not None and (not isinstance(generation, str) or not generation):
        raise ValueError('Invalid served musical group generation')
    versions = {}
    for field, version_field in (('musical_groups', 'version'), ('musical_definitions', 'version'),
                                 ('groups', 'definition_version')):
        values = context.get(field, [])
        if not isinstance(values, list):
            raise ValueError('Invalid musical group evidence references')
        for value in values:
            if not isinstance(value, dict):
                raise ValueError('Invalid musical group evidence definition')
            version = value.get(version_field)
            if version is None and field == 'groups':
                continue  # Historical legacy contexts did not all name a definition.
            group = value.get('group_id') if field != 'groups' else None
            if (not isinstance(version, str) or not version
                    or group is not None and (not isinstance(group, str) or not group)):
                raise ValueError('Invalid external musical group evidence identifier')
            prior = versions.get(version)
            if prior is not None and group is not None and prior != group:
                raise ValueError('Conflicting musical group evidence identity')
            versions[version] = group if group is not None else prior
    return generation, sorted(versions.items())


@dataclass(frozen=True)
class Owner:
    kind: str
    identity: str
    generation: str | None
    versions: tuple
    digest: str
    created_at: float


def prepare(kind, identity, context, created_at):
    if (kind not in KINDS or not isinstance(identity, str) or not identity
            or type(created_at) not in (int, float) or not math.isfinite(created_at)):
        raise ValueError('Invalid musical group evidence owner')
    generation, versions = references(context)
    digest = hashlib.sha256(canonical([generation, versions])).hexdigest()
    return Owner(kind, identity, generation, tuple(versions), digest, created_at)


def retain(conn, kind, identity, context, created_at, *, strict=True):
    return retain_owner(conn, prepare(kind, identity, context, created_at), strict=strict)


def retain_owner(conn, owner, *, strict=True):
    group_storage._writer(conn)
    kind, identity, generation, versions, digest, created_at = (
        owner.kind, owner.identity, owner.generation, owner.versions, owner.digest, owner.created_at)
    existing = conn.execute('SELECT reference_digest FROM group_evidence_owners WHERE kind=? AND external_key=?',
                            (kind, identity)).fetchone()
    if existing is not None:
        if existing[0] != digest:
            raise ValueError('Musical group evidence owner changed its immutable references')
        if strict and conn.execute('SELECT 1 FROM unresolved_group_evidence WHERE kind=? AND external_key=? LIMIT 1',
                                   (kind, identity)).fetchone():
            raise ValueError('Musical group evidence reference remains historically unresolved')
        return False
    resolved, unresolved = [], []
    generation_id = None
    if generation is not None:
        row = conn.execute('SELECT g.id FROM group_generations g JOIN group_publications p '
                           'ON p.generation_id=g.id WHERE g.external_key=?', (generation,)).fetchone()
        if row is None:
            unresolved.append(('generation', generation, 'missing_generation'))
        else:
            generation_id = row[0]
    for version, expected_group in versions:
        row = conn.execute('SELECT a.group_version_id,g.external_key FROM group_version_aliases a '
            'JOIN group_versions v ON v.id=a.group_version_id JOIN taste_groups g ON g.id=v.group_id '
            'JOIN group_publications p ON p.generation_id=a.first_generation_id '
            'WHERE a.external_version=?', (version,)).fetchone()
        reason = ('missing_version' if row is None else 'wrong_group'
                  if expected_group is not None and row[1] != expected_group else None)
        # Legacy definition references stand alone. Adaptive references must
        # name exactly the alias selected by the frozen served generation.
        if reason is None and expected_group is not None and generation is not None and not conn.execute(
                'SELECT 1 FROM generation_groups WHERE generation_id=? AND external_version=? AND group_version_id=?',
                (generation_id, version, row[0])).fetchone():
            reason = 'wrong_generation'
        if reason is not None:
            unresolved.append(('version', version, reason))
        else:
            resolved.append((version, row[0]))
    if strict and unresolved:
        raise ValueError('Musical group evidence reference cannot be established: ' + unresolved[0][2])
    conn.execute('INSERT INTO group_evidence_owners VALUES (?,?,?,?)', (kind, identity, digest, created_at))
    if generation_id is not None:
        conn.execute('INSERT INTO group_evidence_generations VALUES (?,?,?)', (kind, identity, generation_id))
    conn.executemany('INSERT INTO group_evidence_versions VALUES (?,?,?,?)',
                     ((kind, identity, version, version_id) for version, version_id in resolved))
    conn.executemany('INSERT INTO unresolved_group_evidence VALUES (?,?,?,?,?)',
                     ((kind, identity, *value) for value in unresolved))
    return True


def release(conn, kind, identity):
    """Release only after the owner has independently established retirement.

This performs no age-based inference about an offer, response or comparison.
The foreign-key roots and owner disappear in the caller's single transaction.
"""
    group_storage._writer(conn)
    if kind not in KINDS or not isinstance(identity, str) or not identity:
        raise ValueError('Invalid musical group evidence owner')
    for table in ('group_evidence_versions', 'group_evidence_generations', 'unresolved_group_evidence',
                  'unresolved_group_contexts', 'group_evidence_owners'):
        conn.execute(f'DELETE FROM {table} WHERE kind=? AND external_key=?', (kind, identity))


def retain_unknown_context(conn, kind, identity, source_context, reason, created_at):
    """Preserve a missing historical source without inventing its group owners."""
    group_storage._writer(conn)
    if (kind not in KINDS or not isinstance(identity, str) or not identity
            or not isinstance(source_context, str) or not source_context
            or reason not in {'missing_context', 'invalid_context', 'missing_offer', 'invalid_checkpoint'}
            or type(created_at) not in (int, float) or not math.isfinite(created_at)):
        raise ValueError('Invalid unresolved musical group context')
    digest = hashlib.sha256(canonical(['unresolved-context', source_context, reason])).hexdigest()
    existing = conn.execute('SELECT reference_digest FROM group_evidence_owners WHERE kind=? AND external_key=?',
                            (kind, identity)).fetchone()
    if existing is not None:
        if existing[0] != digest:
            raise ValueError('Musical group evidence owner changed its immutable references')
        return False
    conn.execute('INSERT INTO group_evidence_owners VALUES (?,?,?,?)', (kind, identity, digest, created_at))
    conn.execute('INSERT INTO unresolved_group_contexts VALUES (?,?,?,?)', (kind, identity, source_context, reason))
    return True
