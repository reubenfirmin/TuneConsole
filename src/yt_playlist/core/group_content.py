"""Semantic group content, independent of the generation which publishes it.

External version IDs and timestamps identify historical publications. Neither is
evidence of a changed member set. Parent selection is resolved to stable group
identity here; each generation separately selects that parent's actual version.
"""
import hashlib
from itertools import zip_longest
import json
import math

from .evidence_ledger import canonical

FORMAT = 'musical-group-content-1'
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


def document(raw, label):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError(f'Invalid {label} document or byte budget')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    if len(canonical(value)) > MAX_DOCUMENT_BYTES:
        raise ValueError(f'{label} exceeds its canonical byte budget')
    return value


def input_manifest(value):
    """Retain exact old dependency keys, including unresolved historical keys."""
    if not isinstance(value, dict):
        raise ValueError('Musical group inputs must be an object')
    for key, digest in value.items():
        if (not isinstance(key, str) or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid musical group recording input')
    return value


def score(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Musical group scores must be finite numbers between zero and one')
    return float(value) if value else 0.0


def content_rows(group_id, parent_group_id, definition, policy, members, inputs, resolve, *, _validated_inputs=False):
    """Yield bounded canonical rows, ordered by the original exact member key.

The caller supplies the exact recording resolver, never a name/alias heuristic.
Missing input evidence is explicit None, distinct from a known digest. Keeping
that distinction prevents an old incomplete definition from silently acquiring
new algorithm inputs during deduplication.
"""
    if (not isinstance(group_id, str) or not group_id
            or parent_group_id is not None and (not isinstance(parent_group_id, str) or not parent_group_id)
            or not isinstance(definition, dict) or not isinstance(policy, dict)):
        raise ValueError('Invalid musical group content header')
    if not _validated_inputs:
        input_manifest(inputs)
    yield canonical([FORMAT, group_id, parent_group_id, definition, policy])
    previous, encoded_members, used = None, [], 0
    for key, similarity, support in members:
        if not isinstance(key, str) or previous is not None and key <= previous:
            raise ValueError('Musical group members must have unique, ordered exact keys')
        previous = key
        recording_id = resolve(key)
        if recording_id is not None and (type(recording_id) is not int or recording_id < 1):
            raise ValueError('Invalid canonical recording ID in musical group')
        owner = ['recording', recording_id] if recording_id is not None else ['unresolved', key]
        encoded = canonical([owner, score(similarity), score(support), inputs.get(key)])
        used += len(encoded)
        if used > MAX_DOCUMENT_BYTES:
            raise ValueError('Musical group membership exceeds its content byte budget')
        encoded_members.append((owner[0], owner[1], encoded))
    # Account keys can change spelling after canonical reconciliation. Ordering
    # by the actual owner keeps the same membership independent of that spelling.
    owners = set()
    for kind, owner, encoded in sorted(encoded_members):
        if (kind, owner) in owners:
            raise ValueError('Musical group has duplicate canonical recording owners')
        owners.add((kind, owner))
        yield encoded


def digest(rows):
    hashed = hashlib.sha256()
    for row in rows:
        hashed.update(row)
        hashed.update(b'\n')
    return hashed.hexdigest()


def equal(left, right):
    """Verify actual content before reusing a digest match, without retaining it."""
    missing = object()
    return all(a == b for a, b in zip_longest(left, right, fillvalue=missing))


def prepared_rows(prepared, group, resolve):
    """Apply the semantic contract to the existing bounded publication message."""
    definition = document(group['payload'], 'Musical group definition')
    # One generation can contain hundreds of groups sharing the same large
    # dependency map. Decode/validate that map once, rather than once per group.
    policy = _prepared_document(prepared, 'policy', 'Musical group policy')
    payload = _prepared_document(prepared, 'payload', 'Musical group generation')
    inputs = payload.get('recording_inputs', {})
    parent = group.get('parent_version')
    if parent is None:
        parent_group_id = None
    else:
        parents = [item['group_id'] for item in prepared['groups'] if item['version'] == parent]
        if len(parents) != 1:
            raise ValueError('Musical group parent must belong to the same generation')
        parent_group_id = parents[0]
    yield from content_rows(group['group_id'], parent_group_id, definition, policy,
                            sorted(group['members']), inputs, resolve, _validated_inputs=True)


def _prepared_document(prepared, field, label):
    cache_key = '_content_' + field
    cached = prepared.get(cache_key)
    if cached is None or cached[0] != prepared[field]:
        value = document(prepared[field], label)
        if field == 'payload':
            input_manifest(value.get('recording_inputs', {}))
        prepared[cache_key] = (prepared[field], value)
    return prepared[cache_key][1]
