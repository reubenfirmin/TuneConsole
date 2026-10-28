"""Reconcile explicit memberships, retain parents and serialize immutable generations."""
import uuid

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import group_names, musical_profiles


def fingerprint(value):
    return musical_profiles.digest(value)


def recording_inputs(recordings):
    """Musical inputs only: operational timestamps do not change membership."""
    return {r.key: fingerprint([r.genre, r.year, r.values, r.support, r.profile_version]) for r in recordings}


def legacy_definition(mode):
    """Stable reference also embedded in actual legacy offers; membership is explicitly unknown."""
    value = {key: mode[key] for key in ('mode_id', 'label', 'families', 'space', 'rep_keys', 'size')}
    value['centroid'] = np.asarray(mode['centroid'], dtype=np.float32).tolist()
    value.update(legacy=True, membership_known=False)
    return 'legacy-' + fingerprint(value), value


def reconcile(nodes, previous, *, threshold=.8, identity_seed=None):
    """One-to-one continuation by two-sided full membership overlap, never display name.

    A split's broad node matches its former leaf and retains that identity. Its
    children acquire new IDs. A merge keeps an existing umbrella when one exists.
    """
    pairs = []
    prior_members = [(old, set(old.get('members', {}))) for old in previous]
    for node in nodes:
        keys = set(node['members'])
        for old, members in prior_members:
            overlap = len(keys & members)
            score = min(overlap / max(1, len(keys)), overlap / max(1, len(members)))
            if score >= threshold:
                pairs.append((-score, abs(node['depth'] - old.get('depth', 0)), -overlap, node['index'], old['group_id']))
    used, matched = set(), set()
    for _score, _depth, _overlap, index, group_id in sorted(pairs):
        if index not in matched and group_id not in used:
            nodes[index]['group_id'] = group_id
            matched.add(index)
            used.add(group_id)
    for node in nodes:
        node.setdefault('group_id', uuid.uuid5(uuid.NAMESPACE_URL, f'{identity_seed}:{node["index"]}').hex
                        if identity_seed is not None else uuid.uuid4().hex)


def prepare(result, recordings, previous, legacy_modes, *, now, serial, input_fingerprint, policy,
            metadata=None, legacy_versions=(), generation=None):
    now = float(now)  # SQLite's retained REAL timestamp must reconstruct the same serialized plan.
    nodes = result['nodes']
    old = [group for group in (previous or {}).get('groups', []) if not group.get('legacy')]
    reconcile(nodes, old, threshold=policy.continuation, identity_seed=generation)
    names = group_names.name_groups(result, recordings, previous=old, metadata=metadata)
    generation = generation or uuid.uuid4().hex
    groups, lineage = [], []
    versions = {node['index']: fingerprint([generation, node['group_id']]) for node in nodes}
    structural = bool(not previous or
        {(n['group_id'], nodes[n['parent']]['group_id'] if n['parent'] is not None else None, n['leaf']) for n in nodes}
        != {(g['group_id'], next((p['group_id'] for p in old if p['version'] == g['parent_version']), None), g['leaf']) for g in old})
    last_structure_at = now if structural else previous['payload'].get('last_structure_at', now)
    metric = result['metric']
    for node in nodes:
        check_stopping()
        # Representatives are actual members with the most supported links to a bounded
        # evenly sampled member set. Membership never depends on these summaries.
        rows = node['rows']
        anchors = [rows[i] for i in np.linspace(0, len(rows) - 1, min(12, len(rows)), dtype=int)]
        fit = []
        for start in range(0, len(rows), 64):
            check_stopping()
            batch = rows[start:start + 64]
            scores, support = metric.pairs(np.repeat(batch, len(anchors)), np.tile(anchors, len(batch)))
            scores = scores.reshape(len(batch), len(anchors)).mean(axis=1)
            support = support.reshape(len(batch), len(anchors)).mean(axis=1)
            fit.extend((float(score), float(shared), recordings[row].key)
                       for row, score, shared in zip(batch, scores, support))
        reps = [key for _score, _support, key in sorted(fit, key=lambda item: (-item[0], item[2]))[:6]]
        payload = {**names[node['index']], 'depth': node['depth'], 'leaf': node['leaf'],
                   'size': len(rows), 'rep_keys': reps, 'membership_known': True,
                   'legacy': False, 'definition_policy': result['policy']}
        groups.append({'group_id': node['group_id'], 'version': versions[node['index']],
            'parent_version': versions.get(node['parent']), 'payload': musical_profiles.canonical(payload),
            'members': [(key, score, support) for score, support, key in fit]})
    # Lineage is temporal; it is never inferred from the current parent/child edge.
    prior_members = [(source, set(source['members'])) for source in old]
    for node in nodes:
        keys = set(node['members'])
        candidates = []
        for source, old_keys in prior_members:
            overlap = len(keys & old_keys)
            if not overlap:
                continue
            old_share, new_share = overlap / len(old_keys), overlap / len(keys)
            same = source['group_id'] == node['group_id']
            # A leaf can split into new children; child leaves can merge into an umbrella.
            # Do not create temporal mappings between every old ancestor and every new descendant.
            if same or source.get('leaf') and node['leaf'] and min(old_share, new_share) >= .2:
                relation = 'continuation' if same else 'split' if new_share > old_share else 'merge'
                candidates.append((source['version'], versions[node['index']], relation,
                    musical_profiles.canonical({'old_share': old_share, 'new_share': new_share,
                                               'overlap': overlap, 'compatible': min(old_share, new_share) >= policy.continuation})))
        lineage.extend(candidates)
    # Immutable snapshots of legacy definitions coexist with adaptive definitions. They
    # carry no invented member rows and cannot train a specific adaptive child by default.
    already = set(legacy_versions)
    for mode in legacy_modes:
        version, value = legacy_definition(mode)
        if version in already:
            continue
        groups.append({'group_id': f"legacy:{mode['mode_id']}", 'legacy_mode_id': mode['mode_id'],
            'version': version, 'parent_version': None, 'payload': musical_profiles.canonical(value), 'members': []})
    payload = {'schema': 1, 'shadow': True, 'serving_policy': 'legacy', 'last_structure_at': last_structure_at,
               'recording_inputs': recording_inputs(recordings),
               'diagnostics': result['diagnostics'], 'counts': {'known': sum(len(n['members']) for n in nodes if n['leaf']),
                   'unknown': len(result['unknown']), 'groups': len(nodes), 'edges': len(result['edges'])}}
    return {'generation': generation, 'policy': musical_profiles.canonical(result['policy']),
            'fingerprint': input_fingerprint, 'at': now, 'serial': serial,
            'payload': musical_profiles.canonical(payload), 'groups': groups, 'lineage': lineage,
            'unknown': [(key, 'insufficient-shared-evidence') for key in result['unknown']],
            'edges': [(recordings[a].key, recordings[b].key, weight, support)
                      for a, b, weight, support in result['edges']]}
