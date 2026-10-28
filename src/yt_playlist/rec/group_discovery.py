"""Bounded musical graph components, with diagnostic comparisons for each cut.

Supported edge strength determines graph membership, including singleton regions.
Contrast and perturbation measurements describe cuts; they do not freeze a weak
connection in place. The recursive baseline retains its experimental split gates.
"""
from dataclasses import asdict, dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from yt_playlist.core.activity import check_stopping, WorkBudgetExceeded
from yt_playlist.rec import group_similarity, taste_modes


@dataclass(frozen=True)
class Policy:
    engine: str = 'graph'
    neighbors: int = 16
    candidates: int = 128
    style_weight: float = 1.
    audio_cap: float = .8
    era_penalty: float = 0.
    broad_edge: float = .60
    fine_edge: float = .80
    detail_edge: float = .93
    split_gain: float = .12
    agreement: float = .85
    continuation: float = .8
    assignment_minimum: float = .6
    assignment_margin: float = .04
    assignment_support: float = .3
    max_depth: int = 3
    max_groups: int = 512

    def manifest(self):
        return {'membership': 'supported-edge-components-1',
                'similarity': group_similarity.POLICY, 'audio_only_support': group_similarity.AUDIO_ONLY_SUPPORT,
                **asdict(self)}


def agreement(a, b):
    """Adjusted Rand agreement, including degenerate one-group partitions."""
    a, b = np.asarray(a), np.asarray(b)
    if len(a) < 2:
        return 1.
    _, a = np.unique(a, return_inverse=True)
    _, b = np.unique(b, return_inverse=True)
    table = np.bincount(a * (b.max() + 1) + b, minlength=(a.max() + 1) * (b.max() + 1))
    table = table.reshape(a.max() + 1, b.max() + 1)
    pairs = lambda values: float(np.sum(values * (values - 1) / 2))
    joint, left, right = pairs(table), pairs(table.sum(axis=1)), pairs(table.sum(axis=0))
    expected = left * right / (len(a) * (len(a) - 1) / 2)
    denominator = .5 * (left + right) - expected
    return (joint - expected) / denominator if denominator else 1.


def _partition(rows, edges, threshold):
    positions = {row: i for i, row in enumerate(rows)}
    a, b = [], []
    for left, right, weight, _support in edges:
        if weight >= threshold and left in positions and right in positions:
            a.append(positions[left])
            b.append(positions[right])
    matrix = coo_matrix((np.ones(len(a)), (a, b)), shape=(len(rows), len(rows))).tocsr()
    _count, labels = connected_components(matrix, directed=False)
    return labels


def _gain(metric, rows, labels):
    """Equal-child musical contrast protects supported minority regions from size dilution."""
    groups = [np.asarray(rows)[labels == label] for label in np.unique(labels)]
    gains, supports = [], []
    for group in groups:
        check_stopping()
        # Fixed evenly spaced samples bound validation work without favoring large children.
        own = group[np.linspace(0, len(group) - 1, min(12, len(group)), dtype=int)]
        own_set = set(map(int, group))
        outside = np.array([row for row in rows if row not in own_set], dtype=int)
        outside = outside[np.linspace(0, len(outside) - 1, min(48, len(outside)), dtype=int)]
        left, right = np.repeat(own, len(own)), np.tile(own, len(own))
        peers = left != right
        scores, support = metric.pairs(left[peers], right[peers])
        within = scores[support > 0]
        scores, coverage = metric.pairs(np.repeat(own, len(outside)), np.tile(outside, len(own)))
        between = scores[coverage > 0]
        # Singleton style islands can represent albums/recordings, but cannot
        # claim measured internal cohesion. Their label provides the only support.
        coherence = float(np.mean(within)) if len(within) else (1. if metric.recordings[int(own[0])].genre else 0.)
        contrast = coherence - float(np.mean(between)) if len(between) else 0.
        gains.append(contrast)
        supports.append(float(np.mean(coverage)) if len(coverage) else 0.)
    return float(np.mean(gains)), min(supports, default=0.)


def discover(recordings, *, policy=Policy()):
    if len(recordings) * (policy.candidates * 48 + 2048) > 128 * 1024 * 1024:
        raise WorkBudgetExceeded('Musical neighbor working set exceeds 128 MiB')
    metric, edges = group_similarity.graph(recordings, neighbors=policy.neighbors, candidates=policy.candidates,
        style_weight=policy.style_weight, audio_cap=policy.audio_cap, era_penalty=policy.era_penalty)
    known = [i for i, r in enumerate(recordings) if r.known]
    unknown = [r.key for r in recordings if not r.known]
    nodes, diagnostics = [], []
    projection = metric.projection() if policy.engine == 'recursive' else None

    def visit(rows, parent, depth, local_edges):
        check_stopping()
        if len(nodes) >= policy.max_groups:
            raise WorkBudgetExceeded('Musical hierarchy exceeds group work ceiling')
        index = len(nodes)
        node = {'index': index, 'parent': parent, 'depth': depth, 'rows': rows,
                'members': [recordings[i].key for i in rows], 'leaf': True}
        nodes.append(node)
        if len(rows) < 2 or depth >= policy.max_depth:
            return
        threshold = (policy.broad_edge, policy.fine_edge, policy.detail_edge)[min(depth, 2)]
        if policy.engine == 'graph':
            # A single broad region can still contain useful sound-based refinements.
            # Do not stop just because the broadest threshold leaves it connected.
            for threshold in (policy.broad_edge, policy.fine_edge, policy.detail_edge)[min(depth, 2):]:
                labels = _partition(rows, local_edges, threshold)
                if len(set(map(int, labels))) > 1:
                    break
            other = _partition(rows, local_edges, threshold + .03)
            stable = min(agreement(labels, other), agreement(labels, _partition(rows, local_edges, threshold - .03)))
        elif policy.engine == 'recursive':
            labels, _ = taste_modes._kmeanspp(projection[rows].astype(np.float64), 2, 1234567)
            other, _ = taste_modes._kmeanspp(projection[rows].astype(np.float64), 2, 2345678)
            stable = agreement(labels, other)
        else:
            raise ValueError('Unknown grouping engine')
        count = len(set(map(int, labels)))
        if count < 2:
            return
        gain, support = _gain(metric, rows, labels)
        accepted = policy.engine == 'graph' or (
            gain >= policy.split_gain and stable >= policy.agreement and support > 0)
        diagnostic = {'node': index, 'children': count, 'gain': gain, 'agreement': stable,
                      'support': support, 'accepted': accepted, 'threshold': threshold}
        diagnostics.append(diagnostic)
        # Reserve space for siblings before descending; reaching a work ceiling defers
        # the complete proposal, rather than publishing whichever branch ran first.
        if accepted and len(nodes) + count > policy.max_groups:
            raise WorkBudgetExceeded('Musical hierarchy exceeds group work ceiling')
        if accepted:
            node['leaf'] = False
            buckets = {}
            edges_by_label = {}
            row_labels = dict(zip(rows, map(int, labels)))
            for row, label in row_labels.items():
                buckets.setdefault(label, []).append(row)
            # Children only inspect their own edges. Rescanning the complete
            # library graph for every small component made dynamic cuts quadratic.
            for edge in local_edges:
                label = row_labels[edge[0]]
                if label == row_labels[edge[1]]:
                    edges_by_label.setdefault(label, []).append(edge)
            for label in sorted(set(map(int, labels))):
                visit(buckets[label], index, depth + 1, edges_by_label.get(label, []))

    if known:
        visit(known, None, 0, edges)
    return {'nodes': nodes, 'edges': edges, 'unknown': unknown, 'diagnostics': diagnostics,
            'policy': policy.manifest(), 'metric': metric}


def summary(result):
    nodes = result['nodes']
    leaves = [node for node in nodes if node['leaf']]
    return {'groups': len(nodes), 'leaves': len(leaves), 'assigned': sum(len(n['rows']) for n in leaves),
            'unknown': len(result['unknown']), 'edges': len(result['edges']),
            'sizes': sorted(len(n['rows']) for n in leaves),
            'splits': result['diagnostics']}
