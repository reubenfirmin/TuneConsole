"""Repeatable offline comparisons; never publish a model or touch user feedback."""
from dataclasses import replace
from collections import Counter
import time

import numpy as np

from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.rec import group_discovery, group_names, group_similarity, group_versions, taste_modes


def _labels(result, size):
    labels = np.full(size, -1, dtype=int)
    for node in result['nodes']:
        if node['leaf']:
            labels[node['rows']] = node['index']
    return labels


def _global(recordings, policy):
    metric = group_similarity.Similarity(recordings, style_weight=policy.style_weight,
        audio_cap=policy.audio_cap, era_penalty=policy.era_penalty)
    known = [i for i, r in enumerate(recordings) if r.known]
    projection = metric.projection()[known].astype(np.float64)
    candidates = sorted({min(len(known), count) for count in (2, 4, 8, 14, 20, 28, 40) if len(known)})
    fits = []
    for count in candidates:
        labels, centers = taste_modes._kmeanspp(projection, count, 1234567)
        distortion = float(np.mean(np.square(projection - centers[labels]).sum(axis=1)))
        # An explicitly uncalibrated penalized-distortion global-count baseline.
        criterion = distortion + .05 * count * np.log(max(2, len(known))) / max(1, len(known))
        fits.append((criterion, labels, count))
    labels = min(fits, key=lambda fit: fit[0])[1] if fits else np.array([], dtype=int)
    nodes = [{'index': 0, 'parent': None, 'depth': 0, 'rows': known,
              'members': [recordings[i].key for i in known], 'leaf': len(set(labels)) < 2}] if known else []
    if len(set(labels)) > 1:
        for label in sorted(set(labels)):
            rows = [row for row, value in zip(known, labels) if value == label]
            nodes.append({'index': len(nodes), 'parent': 0, 'depth': 1, 'rows': rows,
                          'members': [recordings[i].key for i in rows], 'leaf': True})
    return {'nodes': nodes, 'edges': [], 'metric': metric, 'unknown': [r.key for r in recordings if not r.known],
            'diagnostics': [], 'policy': {**policy.manifest(), 'engine': 'penalized-global-count'}}


def compare(recordings, *, policy=group_discovery.Policy(), growth=True):
    result = {}
    genres = Counter(r.genre for r in recordings if r.genre)
    minority = {genre for genre, count in genres.items() if count <= 16}
    for engine in ('graph', 'recursive', 'global'):
        started = time.monotonic()
        fit = _global(recordings, policy) if engine == 'global' else group_discovery.discover(
            recordings, policy=replace(policy, engine=engine))
        labels = _labels(fit, len(recordings))
        names = group_names.name_groups(fit, recordings)
        leaves = [node for node in fit['nodes'] if node['leaf']]
        retained = {genre: any(set(node['members']) == {r.key for r in recordings if r.genre == genre}
                              for node in leaves) for genre in sorted(minority)}
        row = {**group_discovery.summary(fit), 'seconds': time.monotonic() - started,
               'minority_style_retention': retained,
               'named_leaves': sum(names[node['index']]['display'] for node in leaves),
               'names': [{'name': names[node['index']]['name'], 'size': len(node['rows']),
                          'display': names[node['index']]['display']} for node in leaves]}
        if growth and len(recordings) > 1:
            indices = np.array([i for i in range(len(recordings)) if i % 10], dtype=int)
            subset = [recordings[int(i)] for i in indices]
            earlier = _global(subset, policy) if engine == 'global' else group_discovery.discover(
                subset, policy=replace(policy, engine=engine))
            row['growth_ari'] = group_discovery.agreement(labels[indices], _labels(earlier, len(subset)))
        result[engine] = row
    return {'policy': policy.manifest(), 'recordings': len(recordings), 'engines': result,
            'input_fingerprint': group_versions.fingerprint([
                [r.key, r.genre, r.year, r.values, r.support, r.profile_version] for r in recordings]),
            'coverage': {'recognized_style': sum(bool(r.genre) for r in recordings),
                'year': sum(r.year is not None for r in recordings),
                'supported_dimensions': {name: sum(r.support[i] > 0 for r in recordings)
                    for i, name in enumerate(group_similarity.DIMENSIONS)}},
            'limitations': ['Shadow hypotheses; not calibrated listening preference.',
                'Growth is an independently prepared deterministic subset, not chronological future evidence.',
                'No outcomes exist for adaptive menus that were never served.']}


def feature_comparison(recordings, *, policy=group_discovery.Policy(max_groups=512)):
    """Explicit uncalibrated ablations, scored on identical frozen inputs."""
    variants = {'shadow': policy, 'light-era': replace(policy, era_penalty=.05),
                'strong-era': replace(policy, era_penalty=.25),
                'more-audio': replace(policy, audio_cap=1.6, style_weight=.5)}
    result = {}
    for name, candidate in variants.items():
        started = time.monotonic()
        try:
            fit = group_discovery.discover(recordings, policy=candidate)
        except WorkBudgetExceeded as exc:
            result[name] = {'policy': candidate.manifest(), 'seconds': time.monotonic() - started,
                           'deferred': str(exc)}
            continue
        names = group_names.name_groups(fit, recordings)
        result[name] = {**group_discovery.summary(fit), 'policy': candidate.manifest(),
            'seconds': time.monotonic() - started,
            'names': [{'name': names[node['index']]['name'], 'size': len(node['rows']),
                       'display': names[node['index']]['display']} for node in fit['nodes'] if node['leaf']]}
    return result
