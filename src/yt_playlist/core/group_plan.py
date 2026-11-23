"""Validated semantic input for a compact musical-group publication."""
from . import group_content as content, group_documents, group_storage
from .activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.repos import group_publication as publication

def recording_keys(prepared):
    metadata = content.document(prepared['payload'], 'Musical group generation')
    keys, used = set(metadata.get('recording_inputs', {})), len(prepared['payload'].encode())
    if used > group_documents.MAX_BYTES:
        raise WorkBudgetExceeded('Musical group publication exceeds its admitted memory')
    for group in prepared['groups']:
        used += len(group['payload'].encode()) + sum(len(key.encode()) + 32 for key, _a, _b in group['members'])
        if used > group_documents.MAX_BYTES:
            raise WorkBudgetExceeded('Musical group publication exceeds its admitted memory')
        keys.update(key for key, _a, _b in group['members'])
    return sorted(keys)


def prepare(prepared, identities):
    resolve = identities.get
    policy = content.document(prepared['policy'], 'Musical group policy')
    metadata = content.document(prepared['payload'], 'Musical group generation')
    inputs = content.input_manifest(metadata.get('recording_inputs', {}))
    for _left, _right, weight, support in prepared['edges']:
        content.score(weight)
        content.score(support)
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
    publication._header(prepared)
    return inputs, plans, policy, metadata
