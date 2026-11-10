"""Validated semantic input for a compact musical-group publication."""
from . import group_content as content, group_storage
from .activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.repos import group_publication as publication

MAX_PLAN_BYTES = 64 * 1024 * 1024


def recording_keys(prepared):
    metadata = content.document(prepared['payload'], 'Musical group generation')
    keys, used = set(metadata.get('recording_inputs', {})), len(prepared['payload'].encode())
    for group in prepared['groups']:
        used += len(group['payload'].encode()) + sum(len(key.encode()) + 32 for key, _a, _b in group['members'])
        if used > MAX_PLAN_BYTES:
            raise WorkBudgetExceeded('Musical group publication exceeds its admitted memory')
        keys.update(key for key, _a, _b in group['members'])
        keys.update(content.document(group['payload'], 'Musical group definition').get('rep_keys', []))
    keys.update(key for key, _reason in prepared['unknown'])
    keys.update(key for left, right, _a, _b in prepared['edges'] for key in (left, right))
    return sorted(keys)


def prepare(prepared, identities):
    resolve = identities.get
    policy = content.document(prepared['policy'], 'Musical group policy')
    metadata = content.document(prepared['payload'], 'Musical group generation')
    inputs = group_storage.normalized_inputs(metadata.pop('recording_inputs', {}), resolve)
    for _left, _right, weight, support in prepared['edges']:
        content.score(weight)
        content.score(support)
    for _source, _target, _relation, raw in prepared['lineage']:
        lineage(raw)
    parents, groups = {}, set()
    for group in prepared['groups']:
        if group['version'] in parents or group['group_id'] in groups:
            raise ValueError('Duplicate musical group or external version in a generation')
        parents[group['version']] = group.get('parent_version')
        groups.add(group['group_id'])
    for version in parents:
        visited, parent = {version}, parents[version]
        while parent is not None:
            if parent in visited or parent not in parents:
                raise ValueError('Invalid musical group parent graph')
            visited.add(parent)
            parent = parents[parent]
    plans = []
    for position, group in enumerate(prepared['groups']):
        check_cancellable_work()
        value = content.document(group['payload'], 'Musical group definition')
        fields = group_storage.definition_fields(value, group['members'])
        digest = content.digest(content.prepared_rows(prepared, group, resolve))
        plans.append((position, group, value, fields, digest))
    prepared.pop('_stage_digest', None)
    header = publication._header(prepared)
    metadata = content.document(header[-1], 'Musical group generation')
    for field in ('recording_inputs', '_staging_digest', '_staging_parent'):
        metadata.pop(field, None)
    return inputs, plans, policy, metadata


def lineage(raw):
    value = content.document(raw, 'Musical group lineage')
    for field in ('old_share', 'new_share'):
        if value.get(field) is not None:
            content.score(value[field])
    if value.get('compatible') is not None and type(value['compatible']) is not bool:
        raise ValueError('Invalid musical group lineage compatibility')
    if value.get('overlap') is not None and (type(value['overlap']) is not int or value['overlap'] < 0):
        raise ValueError('Invalid musical group lineage overlap')
    return value
