"""Explicit group roots owned by actual offers, responses and prepared contexts.

These rows retain exact external aliases. Resolving two aliases to the same
content never proves that either alias occurred in a particular served context.
Callers publish roots before acknowledging the corresponding external record.
"""
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass

from .evidence_ledger import canonical
from . import group_storage, publication_files, worker_settings

KINDS = frozenset({'offer', 'response', 'comparison', 'prepared', 'pending', 'checkpoint'})
CONTEXTS = 'group_evidence_contexts'
REFERENCES = 'retained_group_evidence'
PREFIX = 'musical_group_evidence:'
CONTEXT_FIELDS = ('reference_digest', 'created_at', 'unresolved_context_source', 'unresolved_context_reason', 'generation_id')
HOME_CONTEXTS = (f"{publication_files.MANIFESTS} display,json_each(display.value,'$.group_roots') context "
                 f"WHERE display.key GLOB '{publication_files.HOME}*'")
HOME_VERSIONS = (f"{publication_files.MANIFESTS} display,json_each(display.value,'$.group_roots') context,"
                 "json_each(context.value,'$[8]') reference "
                 f"WHERE display.key GLOB '{publication_files.HOME}*' AND reference.value->>1='resolved_version'")


def setting_key(kind, identity):
    return f'{PREFIX}{kind}:{identity}'


def _setting_projection():
    suffix = f'substr(setting.key,{len(PREFIX) + 1})'
    return (f"substr({suffix},1,instr({suffix},':')-1) AS kind,"
            f"substr({suffix},instr({suffix},':')+1) AS external_key")


def setting_contexts():
    values = ','.join(f"setting.value->>'$[{i}]' AS {field}" for i, field in enumerate(CONTEXT_FIELDS))
    return (f"SELECT {_setting_projection()},{values} FROM settings setting WHERE setting.key GLOB '{PREFIX}*'")


def contexts_view():
    return (f'CREATE VIEW {CONTEXTS} AS {setting_contexts()} UNION ALL '
            'SELECT context.value->>1,context.value->>2,context.value->>3,context.value->>4,'
            f'context.value->>5,context.value->>6,context.value->>7 FROM {HOME_CONTEXTS}')


def references_view():
    return (f'CREATE VIEW {REFERENCES} AS SELECT {_setting_projection()},'
            'reference.value->>0 AS reference_key,reference.value->>1 AS reference_kind,'
            'reference.value->>2 AS group_version_id,reference.value->>3 AS reason '
            f"FROM settings setting,json_each(setting.value,'$[5]') reference WHERE setting.key GLOB '{PREFIX}*'")


def entry(conn, kind, identity):
    value = worker_settings.read(conn, setting_key(kind, identity))
    if value is not None:
        return json.loads(value)
    if kind == 'prepared' and identity.startswith('home-output:'):
        row = conn.execute(f'SELECT context.value FROM {HOME_CONTEXTS} AND context.value->>2=? LIMIT 1',
                           (identity,)).fetchone()
        if row is not None:
            return json.loads(row[0])[3:]
    return None


def identities(conn, kind, after, limit):
    prefix = setting_key(kind, '')
    query = 'SELECT substr(key,?) AS identity FROM settings WHERE key>? AND key<?'
    bounds = (len(prefix) + 1, prefix + after, prefix[:-1] + ';')
    order = 'key'
    if kind == 'prepared':
        query += f' UNION ALL SELECT context.value->>2 FROM {HOME_CONTEXTS} AND context.value->>1=? AND context.value->>2>?'
        bounds += (kind, after)
        order = 'identity'
    return [row[0] for row in conn.execute(f'{query} ORDER BY {order} LIMIT ?', (*bounds, limit))]


def _insert(conn, kind, identity, value):
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                 (setting_key(kind, identity), json.dumps(value, separators=(',', ':'), allow_nan=False)))


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


def register_prepared(conn, kind, surface, operations, generations, *, historical=False, owner_prefix=None):
    """Persist explicitly prepared reference rows under their SQL publication owner."""
    from dataclasses import replace
    from . import recommendation_custody, recommendation_references

    source = 'proposal:' + surface
    prefix = owner_prefix or source
    if generations is not None:
        identity = prefix + ':recommendations'
        recommendation_custody.release(conn, kind, identity)
        recommendation_references.retain(conn, kind, identity, generations)
    for identity, owner, unknown, references in operations:
        identity = prefix + identity[len(source):]
        existing = entry(conn, kind, identity)
        if historical and existing is not None:
            continue
        if generations is not None:
            recommendation_custody.release(conn, kind, identity)
        if unknown is not None:
            retain_unknown_context(conn, kind, identity, *unknown)
        elif owner is None:
            release(conn, kind, identity)
        else:
            if existing is not None and existing[0] != owner.digest:
                release(conn, kind, identity)
            retain_owner(conn, replace(owner, identity=identity), strict=not historical and owner.generation is not None)
        if unknown is None and generations is not None:
            recommendation_references.retain(conn, kind, identity, references)


def retain_owner(conn, owner, *, strict=True):
    group_storage._writer(conn)
    kind, identity = owner.kind, owner.identity
    existing = entry(conn, kind, identity)
    if existing is not None:
        if existing[0] != owner.digest:
            raise ValueError('Musical group evidence owner changed its immutable references')
        if strict and any(reference[3] is not None for reference in existing[5]):
            raise ValueError('Musical group evidence reference remains historically unresolved')
        return False
    generation_id, captured = _resolve(conn, owner, strict=strict)
    _insert(conn, kind, identity, [owner.digest, owner.created_at, None, None, generation_id, captured])
    return True


def _resolve(conn, owner, *, strict):
    generation, versions = owner.generation, owner.versions
    captured = []
    generation_id = None
    if generation is not None:
        row = conn.execute('SELECT id FROM group_generations WHERE external_key=? AND is_published=1',
                           (generation,)).fetchone()
        if row is None:
            captured.append((generation, 'generation', None, 'missing_generation'))
        else:
            generation_id = row[0]
    for version, expected_group in versions:
        row = conn.execute('SELECT v.id,g.external_key FROM group_versions v '
            'JOIN taste_groups g ON g.id=v.group_id '
            'JOIN group_generations p ON p.id=v.generation_id AND p.is_published=1 '
            'WHERE v.external_version=? AND v.is_origin=1', (version,)).fetchone()
        reason = ('missing_version' if row is None else 'wrong_group'
                  if expected_group is not None and row[1] != expected_group else None)
        # Legacy definition references stand alone. Adaptive references must
        # name exactly the alias selected by the frozen served generation.
        if reason is None and expected_group is not None and generation is not None:
            selected = conn.execute('SELECT id FROM group_versions WHERE generation_id=? AND external_version=? '
                                    'AND position IS NOT NULL', (generation_id, version)).fetchone()
            if selected is None:
                reason = 'wrong_generation'
            else:
                row = selected
        if reason is not None:
            captured.append((version, 'version', None, reason))
        else:
            captured.append((version, 'resolved_version', row[0], None))
    if strict:
        first_error = next((reason for _, _, _, reason in captured if reason is not None), None)
        if first_error:
            raise ValueError('Musical group evidence reference cannot be established: ' + first_error)
    return generation_id, sorted(captured, key=lambda value: (value[1], value[0]))


def display_contexts(conn, kind, publication_id, operations):
    rows = []
    for name, owner, unknown, _ in operations:
        field = name.rsplit(':', 1)[1]
        key = f'home-output:{publication_id}:{field}'
        if unknown is not None:
            source, reason, at = unknown
            rows.append((field, kind, key, _unresolved_digest(source, reason), at, source, reason, None, []))
        elif owner is not None:
            generation, captured = _resolve(conn, owner, strict=owner.generation is not None)
            rows.append((field, kind, key, owner.digest, owner.created_at, None, None, generation, captured))
    return sorted(rows)


def release(conn, kind, identity):
    """Release only after the owner has independently established retirement.

This performs no age-based inference about an offer, response or comparison.
The root holds and owner disappear in the caller's single transaction.
"""
    group_storage._writer(conn)
    if kind not in KINDS or not isinstance(identity, str) or not identity:
        raise ValueError('Invalid musical group evidence owner')
    if kind == 'prepared' and identity.startswith('home-output:') and conn.execute(
            f'SELECT 1 FROM {HOME_CONTEXTS} AND context.value->>2=? LIMIT 1', (identity,)).fetchone():
        raise sqlite3.IntegrityError('Home display retains this reference')
    worker_settings.save(conn, setting_key(kind, identity), None)


def _unresolved_digest(source, reason):
    return hashlib.sha256(canonical(['unresolved-context', source, reason])).hexdigest()


def retain_unknown_context(conn, kind, identity, source_context, reason, created_at):
    """Preserve a missing historical source without inventing its group owners."""
    group_storage._writer(conn)
    if (kind not in KINDS or not isinstance(identity, str) or not identity
            or not isinstance(source_context, str) or not source_context
            or reason not in {'missing_context', 'invalid_context', 'missing_offer', 'invalid_checkpoint'}
            or type(created_at) not in (int, float) or not math.isfinite(created_at)):
        raise ValueError('Invalid unresolved musical group context')
    digest = _unresolved_digest(source_context, reason)
    existing = entry(conn, kind, identity)
    if existing is not None:
        if existing[0] != digest:
            raise ValueError('Musical group evidence owner changed its immutable references')
        return False
    _insert(conn, kind, identity, [digest, created_at, source_context, reason, None, []])
    return True
