"""Cheap provisional assignment against the published graph regions, between structural fits.

These are disposable generation-tagged hints, never edits to historical membership.
The next validated generation makes complete membership authoritative. Unknown fits
remain explicit, and shadow assignments never enter live Home or track learning.
"""
import json

import numpy as np

from yt_playlist.core.activity import work_budget, WorkBudgetExceeded, check_stopping
from yt_playlist.rec import group_similarity


def _region_batches(leaves, anchors):
    """Gather equal-length regions without changing their representative order."""
    positions = {anchor.key: i for i, anchor in enumerate(anchors)}
    by_size = {}
    for group in leaves:
        # The original scan visited each available anchor once, in anchor order,
        # even when a group repeated a key or listed its representatives backwards.
        indices = sorted({positions[key] for key in group['rep_keys'][:3] if key in positions})
        if indices:
            versions, regions = by_size.setdefault(len(indices), ([], []))
            versions.append(group['version'])
            regions.append(indices)
    return [(versions, np.asarray(regions, dtype=np.intp))
            for versions, regions in by_size.values()]


def prepare(reader, rows, musical, now, *, seconds=.25):
    try:
        with work_budget(seconds):
            if reader.settings.get_setting('rec_group_shadow') == '0':
                return []
            head = reader.taste_groups.assignment_head()
            if not head:
                return []
            leaves = head['groups']
            reps = sorted({key for group in leaves for key in group['rep_keys'][:3]})
            anchors = []
            for start in range(0, len(reps), 32):
                for key, payload, profile in reader.taste_groups.inputs_for(reps[start:start + 32]):
                    if payload['library'] is not None:
                        anchors.append(group_similarity.recording(key, payload['library'], profile))
            profiles = {key: (json.loads(raw) if raw else None, digest) for key, raw, _version, digest in musical}
            policy = json.loads(head['policy'])
            weights = {name: policy[name] for name in ('style_weight', 'audio_cap', 'era_penalty')}
            features = [json.loads(row[2])['library'] for row in rows]
            candidates = [group_similarity.recording(row[0], value or {},
                                                     profiles.get(row[0], (None, None))[0])
                          for row, value in zip(rows, features)]
            metric = group_similarity.Similarity([*candidates, *anchors], **weights)
            # Compute each candidate/anchor pair once, then retain the same
            # ordered representative means for every leaf, including shared reps.
            regions = _region_batches(leaves, anchors)
            anchor_indices = np.arange(len(candidates), len(candidates) + len(anchors))
            result = []
            for index, (key, _revision, raw, _digest, _changed) in enumerate(rows):
                check_stopping()
                _profile, digest = profiles.get(key, (None, None))
                fits = []
                if features[index] and candidates[index].known:
                    scores, support = metric.pairs(index, anchor_indices)
                    for versions, indices in regions:
                        region_support = support[indices]
                        supported = np.flatnonzero(np.any(region_support > 0, axis=1))
                        region_scores = np.mean(scores[indices], axis=1)
                        region_support = np.mean(region_support, axis=1)
                        fits.extend((float(region_scores[i]), float(region_support[i]), versions[i])
                                    for i in supported)
                fits.sort(reverse=True)
                score, support, version = fits[0] if fits else (0., 0., None)
                if (score < policy['assignment_minimum'] or support < policy['assignment_support']
                        or len(fits) > 1 and score - fits[1][0] < policy['assignment_margin']):
                    version = None
                result.append((key, head['generation'], version, score, support, digest, now))
            return result
    except WorkBudgetExceeded:
        # Optional hints must never delay durable content capture or a Home refresh.
        return []
