"""Fit regularized audio-vibe corrections from internal development annotations.

The popup labels are supervised training/evaluation targets, not listener
preferences or per-song ranking overrides. This offline trainer reads them from
SQLite in read-only mode; runtime inference consumes the exported coefficients
and catalog/audio measurements without looking up raw annotations.

uv run python -m experiments.audio_lab_tools.train_vibes --db /path/state.db \
    --output /tmp/vibe-model.json --report /tmp/vibe-evaluation.json
"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logsumexp
from threadpoolctl import threadpool_limits

from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import audio_vibe_model as model
from yt_playlist.rec import audio_vibe_preview as heuristic
from yt_playlist.core import audio_embedding, audio_vibe_metadata, sample_files

MIN_SUPPORT = 3
WIDE_DIVERGENCE_GAP = .5
# Smooth upper bound on the L2 norm; the robust margin differs from the exact
# worst-case margin by at most radius * 1e-4, including at zero coefficients.
ADVERSARIAL_SMOOTHING = 1e-4
CANDIDATES = {'heuristic': None, 'intercept': {'intercept_only': True},
              'ridge_1': {'regularization': 1.}, 'ridge_10': {'regularization': 10.},
              'libra_1': {'regularization': 1., 'objective': 'acceptable_libra'},
              'libra_10': {'regularization': 10., 'objective': 'acceptable_libra'}}


def track_samples(conn, videos, skipped, *, require_embeddings=False):
    """Latest compatible measurement at each sampled point, across all playbacks."""
    samples, signatures = {}, {}
    videos = sorted(videos)
    for start in range(0, len(videos), 400):
        batch = videos[start:start + 400]
        for row in conn.execute('SELECT * FROM sample_attributes WHERE video_id IN ('
                                + ','.join('?' for _ in batch) + ') ORDER BY captured_at DESC,id DESC', batch):
            video, point = row['video_id'], row['point']
            if point in samples.get(video, {}):
                continue
            if point not in ('thirty', 'middle', 'two_thirds'):
                skipped['invalid_sample_point'] += 1
                continue
            try:
                value = sample_files.read(row['filename'], expected={name: row[name]
                    for name in ('id', 'run_id', 'point', 'video_id')})
            except (OSError, ValueError):
                skipped['unavailable_sample_files'] += 1
                continue
            if require_embeddings:
                try:
                    audio_embedding.signature(value)
                except ValueError:
                    skipped['missing_or_invalid_embedding'] += 1
                    continue
            try:
                # A captured excerpt is useful even before the other points
                # arrive. Check its actual inputs, without manufacturing copies.
                baseline = heuristic.predict([value], use_model=False, require_complete=False)
                model.vector([value], baseline)
                signature = model.signature(value)
            except (ValueError, KeyError, TypeError):
                skipped['incompatible_measurements'] += 1
                continue
            if video in signatures and signature != signatures[video]:
                skipped['earlier_sample_model_version'] += 1
                continue
            signatures[video] = signature
            samples.setdefault(video, {})[point] = (value, row['captured_at'])
    return samples, signatures


def examples_from_db(path, *, require_embeddings=False):
    """Join track feedback and track measurements independently of playback."""
    skipped = Counter()
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        labels = list(conn.execute('SELECT labels.*,COALESCE(runs.video_id,'
            '(SELECT video_id FROM sample_attributes WHERE run_id=labels.run_id '
            'ORDER BY captured_at DESC,id DESC LIMIT 1)) video_id '
            'FROM audio_session_labels labels LEFT JOIN audio_vibe_runs runs USING(run_id) '
            'WHERE labels.taxonomy_version=? ORDER BY labels.updated_at,labels.run_id', (TAXONOMY_VERSION,)))
        earlier = conn.execute('SELECT COUNT(*) FROM audio_vibe_feedback').fetchone()[0]
        earlier += conn.execute('SELECT COUNT(*) FROM audio_vibe_labels').fetchone()[0]
        earlier += conn.execute('SELECT COUNT(*) FROM audio_session_labels WHERE taxonomy_version!=?',
                                (TAXONOMY_VERSION,)).fetchone()[0]
        if earlier:
            skipped['earlier_label_vocabulary'] = earlier
        latest = {}
        for label in labels:
            if not label['video_id']:
                skipped['unidentified_feedback_track'] += 1
                continue
            if label['video_id'] in latest:
                skipped['earlier_answer_for_same_song'] += 1
            latest[label['video_id']] = label
        selected = {}
        for video, label in latest.items():
            try:
                targets = json.loads(label['vibes'])
                valid = (isinstance(targets, list) and len(set(targets)) == len(targets)
                         and set(targets) <= set(model.LABELS))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                skipped['invalid_labels'] += 1
            elif not targets:
                skipped['empty_or_cleared'] += 1
            else:
                selected[video] = targets
        samples, signatures = track_samples(conn, selected, skipped, require_embeddings=require_embeddings)
        # Pin catalog values and excerpts to the same read-only DB transaction.
        metadata = {video: audio_vibe_metadata.read(conn, video) for video in sorted(samples)}
    records = []
    for video, targets in selected.items():
        if video not in samples:
            skipped['no_available_samples'] += 1
            continue
        label = latest[video]
        measurements = sorted(samples[video].values(), key=lambda pair: (
            pair[0]['position_seconds'], pair[0]['target_seconds'], pair[0]['id']))
        excerpts = [value for value, captured_at in measurements]
        try:
            baseline = heuristic.predict(excerpts, use_model=False, require_complete=False)
            values = model.vector(excerpts, baseline, metadata[video])
        except (ValueError, KeyError, TypeError):
            skipped['incompatible_measurements'] += 1
            continue
        embedding = {}
        if all(row.get('embedding') is not None for row in excerpts):
            try:
                embedding = {'embedding_values': audio_embedding.vector(excerpts),
                             'embedding_signature': audio_embedding.signature(excerpts[0])}
            except ValueError:
                skipped['incompatible_embeddings'] += 1
                if require_embeddings:
                    continue
        records.append({**embedding, 'video_id': video, 'feedback_run_id': label['run_id'],
                        'sample_ids': [row['id'] for row in excerpts],
                        'sample_run_ids': [row['run_id'] for row in excerpts],
                        'sample_points': [row['point'] for row in excerpts],
                        'sampled_at': [captured_at for value, captured_at in measurements],
                        'title': excerpts[0].get('title', ''), 'artist': excerpts[0].get('artist', ''),
                        'labels': targets, 'values': values,
                        'feedback_prediction': feedback_snapshot(json.loads(label['prediction']), targets),
                        'metadata_sha256': hashlib.sha256(json.dumps(
                            metadata[video], sort_keys=True, allow_nan=False).encode()).hexdigest(),
                        'subgenre': audio_vibe_metadata.subgenre(metadata[video]),
                        'baseline': {row['vibe']: row['score'] for row in baseline['ranking']},
                        'source_signature': signatures[video], 'updated_at': label['updated_at']})
    if len({row['source_signature'] for row in records}) > 1:
        raise ValueError('Upstream model versions differ; use a snapshot with one measurement/model version')
    return records, dict(skipped)


def example_vector(example, feature_names):
    values = dict(zip(model.FEATURES, example['values'], strict=True))
    if any(name.startswith('embedding.') for name in feature_names):
        embedding = example.get('embedding_values')
        if (not isinstance(embedding, list) or len(embedding) != audio_embedding.DIMENSIONS
                or not all(model.finite(value) for value in embedding)):
            raise ValueError('This comparison requires captured embeddings on every track')
        values.update(zip(audio_embedding.FEATURES, embedding, strict=True))
    values.update({audio_vibe_metadata.SUBGENRE_PREFIX + name: float(example.get('subgenre') == name)
                   for name in audio_vibe_metadata.vocabulary(feature_names)})
    return [values[name] for name in feature_names]


def logistic_objective(weights, design, offset, targets, regularization, *,
                       sample_weights=None, attack_mask=None, adversarial_radius=0.):
    """Smooth upper bound on worst-case logistic loss in observed feature space.

    The intercept, missing values and categorical identities cannot be attacked.
    The heuristic offset stays fixed; this controls sensitivity of the learned
    correction, rather than claiming robustness to arbitrary waveform changes.
    """
    signed = 2 * targets - 1
    losses = -signed * (offset + design @ weights)
    if adversarial_radius:
        attacked = attack_mask * weights
        norm = np.sqrt((attacked ** 2).sum(axis=1) + ADVERSARIAL_SMOOTHING ** 2)
        losses = losses + adversarial_radius * norm
    importance = np.ones(len(targets)) if sample_weights is None else sample_weights
    slope = importance * expit(losses)
    loss = importance @ np.logaddexp(0, losses) + .5 * regularization * (weights @ weights)
    gradient = design.T @ (-signed * slope) + regularization * weights
    if adversarial_radius:
        gradient += adversarial_radius * (slope / norm) @ (attack_mask * attacked)
    return loss, gradient


def training_design(examples, feature_set='combined', intercept_only=False):
    if not examples:
        raise ValueError('No sampled tracks with developer feedback are available')
    if feature_set not in ('combined', 'audio_only', 'embeddings_only', 'embeddings_combined'):
        raise ValueError('Unknown listening feature set')
    # Vocabulary is fitted inside each fold. A unique held-out subgenre has no
    # learned feature, and broad parent families are never added as predictors.
    subgenres = (sorted({row['subgenre'] for row in examples if row.get('subgenre')})
                if feature_set in ('combined', 'embeddings_combined') else [])
    base = (audio_embedding.FEATURES if feature_set == 'embeddings_only' else model.FEATURES
            + (audio_embedding.FEATURES if feature_set == 'embeddings_combined' else ()))
    names = base + tuple(audio_vibe_metadata.SUBGENRE_PREFIX + name for name in subgenres)
    x = np.array([example_vector(row, names) for row in examples], dtype=float)
    if x.shape != (len(examples), len(names)) or np.isinf(x).any():
        raise ValueError('Invalid listening training features')
    observed = ~np.isnan(x)
    count = observed.sum(axis=0)
    # Missing values never become fabricated measurements. Statistics use only
    # this fold's observed values; an entirely unknown column stays inactive.
    center = np.divide(np.where(observed, x, 0).sum(axis=0), count,
                       out=np.zeros(x.shape[1]), where=count > 0)
    variance = np.divide(np.where(observed, (x - center) ** 2, 0).sum(axis=0), count,
                         out=np.zeros(x.shape[1]), where=count > 0)
    scale = np.sqrt(variance)
    scale[scale < 1e-6] = 1
    z = np.where(observed, (x - center) / scale, 0)
    if feature_set == 'audio_only':
        center[len(model.AUDIO_FEATURES):] = 0
        scale[len(model.AUDIO_FEATURES):] = 1
        z[:, len(model.AUDIO_FEATURES):] = 0
    if intercept_only:
        z = np.zeros_like(z)
    design = np.column_stack((np.ones(len(examples)), z))
    continuous = [not name.startswith(('base.present.', 'base.key.', 'base.scale.',
                                      audio_vibe_metadata.SUBGENRE_PREFIX)) for name in names]
    attack_mask = np.column_stack((np.zeros(len(examples)),
                                   observed & np.array(continuous) & (variance > 1e-12)))
    if feature_set == 'audio_only':
        attack_mask[:, 1 + len(model.AUDIO_FEATURES):] = False
    if intercept_only:
        attack_mask[:] = False
    return names, center, scale, design, attack_mask


def acceptable_objective(weights, design, offset, allowed, regularization, *, loss='acceptable_nll',
                         sample_weights=None):
    """One prediction may match ANY acceptable alternative, without set penalties.

    NLL is logsumexp(all logits) - logsumexp(acceptable logits). Libra is
    logsumexp(unacceptable logits) - mean(acceptable logits), from Zombori et al.
    (JMLR 2024). An all-acceptable answer constrains neither loss.
    """
    coefficients = weights.reshape(design.shape[1], offset.shape[1])
    logits = offset + design @ coefficients
    constrained = ~allowed.all(axis=1)
    logits, allowed = logits[constrained], allowed[constrained]
    if loss == 'acceptable_nll':
        total = logsumexp(logits, axis=1, keepdims=True)
        selected = logsumexp(np.where(allowed, logits, -np.inf), axis=1, keepdims=True)
        losses = (total - selected).ravel()
        delta = np.exp(logits - total) - np.exp(np.where(allowed, logits - selected, -np.inf))
    elif loss == 'acceptable_libra':
        rejected = logsumexp(np.where(~allowed, logits, -np.inf), axis=1, keepdims=True)
        uniform = allowed / allowed.sum(axis=1, keepdims=True)
        losses = rejected.ravel() - (uniform * logits).sum(axis=1)
        delta = np.exp(np.where(~allowed, logits - rejected, -np.inf)) - uniform
    else:
        raise ValueError('Unknown acceptable-alternative loss')
    if sample_weights is not None:
        importance = np.asarray(sample_weights)[constrained]
        losses = losses * importance
        delta = delta * importance[:, None]
    value = losses.sum() + .5 * regularization * (weights @ weights)
    gradient = design[constrained].T @ delta + regularization * coefficients
    return value, gradient.ravel()


def validate_unlabeled(examples, unlabeled):
    """An evaluated song cannot reappear as an unlabeled observation."""
    known = {row['video_id'] for row in examples}
    signatures = {row['source_signature'] for row in examples}
    seen = set()
    for row in unlabeled:
        video = row['video_id']
        if video in known or video in seen:
            raise ValueError('Unlabeled tracks must be distinct and disjoint from evaluated tracks')
        if (row.get('labels') or row.get('feedback_prediction')
                or row['source_signature'] not in signatures
                or len(row['values']) != len(model.FEATURES)
                or any(value is not None and not model.finite(value) for value in row['values'])
                or set(row['baseline']) != set(model.LABELS)
                or any(not model.finite(value) or not 0 <= value <= 1 for value in row['baseline'].values())):
            raise ValueError('Invalid unlabeled track measurements: ' + video)
        seen.add(video)


def validate_silver(examples, silver):
    """Silver targets are distinct model predictions, never human answers."""
    validate_unlabeled(examples, [{key: value for key, value in row.items() if key != 'labels'}
                                for row in silver])
    for row in silver:
        labels = row.get('labels')
        if (not isinstance(labels, list) or len(labels) != 1 or labels[0] not in model.LABELS
                or row.get('target_source') != 'model_prediction'):
            raise ValueError('Silver tracks require one explicit model-predicted target')


def manifold_terms(examples, unlabeled, names, center, scale, baseline_weight, neighbors):
    """A bounded similarity graph using measurements, never predicted targets.

    Standardization and vocabulary come from the labeled training fold. Its
    observations and the external unlabeled pool form the graph; validation
    songs are absent entirely, including their feature statistics and edges.
    The quadratic penalizes differences between neighboring total logits.
    """
    validate_unlabeled(examples, unlabeled)
    rows = list(examples) + list(unlabeled)
    if not 2 <= len(rows) <= 2048 or type(neighbors) is not int or not 1 <= neighbors <= 32:
        raise ValueError('Similarity graphs require 2–2048 tracks and 1–32 neighbors')
    x = np.array([example_vector(row, names) for row in rows], dtype=float)
    z = np.where(np.isnan(x), 0., (x - center) / scale)
    if not np.isfinite(z).all():
        raise ValueError('Invalid similarity graph measurements')
    distances = np.maximum(0., (z * z).sum(axis=1)[:, None]
                           + (z * z).sum(axis=1)[None, :] - 2 * z @ z.T)
    np.fill_diagonal(distances, np.inf)
    count = min(neighbors, len(rows) - 1)
    closest = np.argsort(distances, axis=1, kind='stable')[:, :count]
    pairs = np.unique(np.sort(np.column_stack((np.repeat(np.arange(len(rows)), count),
                                              closest.ravel())), axis=1), axis=0)
    left, right = pairs.T
    squared = distances[left, right]
    if not np.isfinite(squared).all():
        raise ValueError('Similarity graph distances exceed the numeric range')
    positive = squared[squared > 1e-12]
    bandwidth = float(np.median(positive)) if len(positive) else 1.
    weights = np.exp(-squared / (2 * bandwidth))
    weights /= weights.sum()
    differences = np.column_stack((np.zeros(len(pairs)), z[left] - z[right]))
    offsets = baseline_weight * np.array([[model.margin(row['baseline'][key])
                                          for key in model.LABELS] for row in rows])
    offset_differences = offsets[left] - offsets[right]
    gram = differences.T @ (weights[:, None] * differences)
    linear = differences.T @ (weights[:, None] * offset_differences)
    constant = (weights[:, None] * offset_differences ** 2).sum(axis=0)
    return gram, linear, constant, {'unlabeled_songs': len(unlabeled), 'graph_songs': len(rows),
        'neighbors': neighbors, 'edges': len(pairs), 'bandwidth_squared': bandwidth,
        'normalization': 'labeled_training_fold_only', 'target_source': 'human_labels_only'}


def manifold_objective(weights, gram, linear, constant, strength):
    """Mean squared neighboring-logit differences and their exact gradient."""
    shape = weights.shape
    coefficients = weights.reshape(gram.shape[0], -1)
    linear = np.asarray(linear).reshape(coefficients.shape)
    gradient = gram @ coefficients + linear
    loss = .5 * np.sum(coefficients * (gram @ coefficients)) + np.sum(coefficients * linear)
    loss += .5 * np.sum(constant)
    return strength * loss, (strength * gradient).reshape(shape)


def fit(examples, regularization=1., intercept_only=False, *, feature_set='combined',
        adversarial_radius=0., hard_example_weight=0., objective='acceptable_nll', baseline_weight=1.,
        manifold_weight=0., graph_neighbors=5, unlabeled_examples=(), silver_examples=(), silver_weight=0.):
    if (not np.isfinite([regularization, adversarial_radius, hard_example_weight, baseline_weight]).all()
            or regularization <= 0 or adversarial_radius < 0 or hard_example_weight < 0
            or not 0 <= baseline_weight <= 1):
        raise ValueError('Invalid listening training penalty')
    if objective not in ('acceptable_nll', 'acceptable_libra', 'legacy_binary'):
        raise ValueError('Unknown listening training objective')
    if objective != 'legacy_binary' and (adversarial_radius or hard_example_weight):
        raise ValueError('Binary robustness penalties require the explicit legacy objective')
    if not model.finite(manifold_weight) or manifold_weight < 0:
        raise ValueError('Invalid manifold training penalty')
    if manifold_weight and (intercept_only or adversarial_radius or feature_set != 'combined'):
        raise ValueError('Manifold training currently supports the complete attribute feature set')
    if unlabeled_examples and not manifold_weight:
        raise ValueError('Unlabeled inputs require an explicit manifold penalty')
    if not model.finite(silver_weight) or not 0 <= silver_weight <= 1:
        raise ValueError('Silver weight must be between zero and one')
    if bool(silver_examples) != bool(silver_weight):
        raise ValueError('Silver inputs require a positive silver weight, and vice versa')
    if silver_examples and (manifold_weight or adversarial_radius or hard_example_weight
                            or intercept_only or feature_set != 'combined' or objective == 'acceptable_libra'):
        raise ValueError('Silver training supports ordinary attribute NLL and legacy binary models')
    validate_silver(examples, silver_examples)
    gold = examples
    names, center, scale, design, attack_mask = training_design(examples, feature_set, intercept_only)
    manifold, graph = None, {}
    if manifold_weight:
        *manifold, graph = manifold_terms(examples, unlabeled_examples, names, center, scale,
                                         1. if objective == 'legacy_binary' else baseline_weight, graph_neighbors)
        graph = {'manifold': {**graph, 'weight': manifold_weight}}
    importance = None
    if silver_examples:
        x = np.array([example_vector(row, names) for row in silver_examples], dtype=float)
        z = np.where(np.isnan(x), 0., (x - center) / scale)
        design = np.vstack((design, np.column_stack((np.ones(len(silver_examples)), z))))
        examples = list(gold) + list(silver_examples)
        importance = np.r_[np.ones(len(gold)), np.full(len(silver_examples), silver_weight)]
        graph = {'gold_silver': {'gold_songs': len(gold), 'silver_songs': len(silver_examples),
            'gold_weight': 1., 'silver_weight': silver_weight,
            'effective_training_weight': float(importance.sum()),
            'normalization': 'gold_training_fold_only', 'minimum_support': 'gold_training_fold_only',
            'silver_target': 'teacher_top_one', 'silver_scores_calibrated': False}}
    classifiers = {}
    if objective != 'legacy_binary':
        allowed = np.array([[key in row['labels'] for key in model.LABELS] for row in examples])
        if not allowed.any(axis=1).all():
            raise ValueError('Every training track needs an acceptable answer')
        offset = baseline_weight * np.array([[model.margin(row['baseline'][key])
                                             for key in model.LABELS] for row in examples])

        def joint_objective(weights):
            loss, gradient = acceptable_objective(weights, design, offset, allowed, regularization,
                                                  loss=objective, sample_weights=importance)
            if manifold is not None:
                extra, slope = manifold_objective(weights, *manifold, manifold_weight * len(examples))
                loss, gradient = loss + extra, gradient + slope
            return loss, gradient

        result = minimize(joint_objective, np.zeros(design.shape[1] * len(model.LABELS)), jac=True,
            method='L-BFGS-B', options={'maxiter': 1000, 'ftol': 1e-12})
        if not result.success or not np.isfinite(result.x).all():
            raise ValueError('Could not fit acceptable-alternative model: ' + result.message)
        coefficients = result.x.reshape(design.shape[1], len(model.LABELS))
        for i, key in enumerate(model.LABELS):
            positive = int(allowed[:, i].sum())
            classifiers[key] = {'intercept': float(coefficients[0, i]),
                'weights': coefficients[1:, i].tolist(), 'baseline_weight': baseline_weight,
                'positive_songs': positive, 'unselected_songs': len(examples) - positive}
    for key in model.LABELS if objective == 'legacy_binary' else ():
        y = np.array([key in row['labels'] for row in examples], dtype=float)
        positive, negative = int(y.sum()), int(len(y) - y.sum())
        gold_positive = sum(key in row['labels'] for row in gold)
        if min(gold_positive, len(gold) - gold_positive) < MIN_SUPPORT:
            continue
        offset = np.array([model.margin(row['baseline'][key]) for row in examples])
        # Difficulty weighting is restricted to gold-only candidates. Silver
        # candidates use their explicit tier weight after ordinary binary loss.
        binary_importance = 1 + hard_example_weight * expit(-(2 * y - 1) * offset)
        binary_importance /= binary_importance.mean()
        if importance is not None:
            binary_importance *= importance

        def binary_objective(weights):
            loss, gradient = logistic_objective(weights, design, offset, y, regularization,
                sample_weights=binary_importance, attack_mask=attack_mask, adversarial_radius=adversarial_radius)
            if manifold is not None:
                gram, linear, constant = manifold
                index = model.LABELS.index(key)
                extra, slope = manifold_objective(weights, gram, linear[:, index], constant[index],
                                                   manifold_weight * len(examples))
                loss, gradient = loss + extra, gradient + slope
            return loss, gradient

        result = minimize(binary_objective, np.zeros(design.shape[1]), jac=True, method='L-BFGS-B',
                          options={'maxiter': 2000 if adversarial_radius else 500, 'ftol': 1e-12})
        if not result.success or not np.isfinite(result.x).all():
            raise ValueError('Could not fit listening correction for ' + key + ': ' + result.message)
        classifiers[key] = {'intercept': float(result.x[0]), 'weights': result.x[1:].tolist(),
                            'positive_songs': positive, 'unselected_songs': negative}
    embedding = {}
    if feature_set in ('embeddings_only', 'embeddings_combined'):
        signatures = {row.get('embedding_signature') for row in examples}
        if len(signatures) != 1 or not isinstance(next(iter(signatures)), str):
            raise ValueError('Cannot mix or omit frozen embedding provenance')
        embedding = {'embedding_signature': next(iter(signatures))}
    return {**embedding, 'schema': model.SCHEMA,
            'feature_version': model.EMBEDDING_FEATURE_VERSION if embedding else model.FEATURE_VERSION,
            'score_link': 'sigmoid' if objective == 'legacy_binary' else 'softmax',
            'taxonomy_version': TAXONOMY_VERSION, 'features': list(names),
            'heuristic_version': heuristic.VERSION, 'threshold': model.THRESHOLD,
            'source_signature': examples[0]['source_signature'], 'center': center.tolist(), 'scale': scale.tolist(),
            'classifiers': classifiers, 'training': {**graph, 'songs': len(examples), 'regularization': regularization,
                'intercept_only': intercept_only, 'feature_set': feature_set,
                'objective': objective, 'target_interpretation': ('legacy_independent_binary'
                    if objective == 'legacy_binary' else 'acceptable_alternatives'),
                'adversarial_radius': adversarial_radius, 'hard_example_weight': hard_example_weight,
                'source_weighting': ('gold_normalization_with_weighted_silver_targets' if silver_examples
                                     else 'equal_standardization_and_regularization'),
                'minimum_positive_and_unselected_songs': MIN_SUPPORT if objective == 'legacy_binary' else 0}}


def select(scores):
    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[:1]


def disagreement(scores, human, predicted=None):
    """The displayed choice is correct if it matches any acceptable alternative.

    The gap is on the existing uncalibrated score scale, not a probability or
    a claim that the target names have an ordinal/semantic distance.
    """
    predicted = select(scores) if predicted is None else predicted[:1]
    expected = set(human)
    best_human = max(human, key=scores.get)
    leading = predicted[0]
    gap = round(max(0., scores[leading] - scores[best_human]), 5)
    if leading in expected:
        kind = 'agreement'
    elif gap >= WIDE_DIVERGENCE_GAP:
        kind = 'wide_divergence'
    else:
        kind = 'disjoint'
    return {'class': kind, 'score_gap': round(gap, 5), 'predicted': list(predicted),
            'scores': dict(scores), 'best_human_label': best_human,
            'missed': [] if leading in expected else list(human),
            'extra': [] if leading in expected else list(predicted)}


def feedback_snapshot(prediction, human):
    """Inspect the actual saved hypothesis; never substitute a new prediction."""
    if not isinstance(prediction, dict) or prediction.get('taxonomy_version') != TAXONOMY_VERSION:
        return None
    try:
        ranking, predicted = prediction['ranking'], prediction['vibes']
        scores = {row['vibe']: row['score'] for row in ranking}
        if (len(ranking) != len(model.LABELS) or set(scores) != set(model.LABELS)
                or not all(model.finite(value) and 0 <= value <= 1 for value in scores.values())
                or not isinstance(predicted, list) or not predicted
                or len(set(predicted)) != len(predicted) or not set(predicted) <= set(model.LABELS)):
            return None
        return {**disagreement(scores, human, predicted), 'version': prediction.get('version'),
                'model': prediction.get('model')}
    except (KeyError, TypeError):
        return None


def metrics(predictions):
    by_label = {key: Counter() for key in model.LABELS}
    for row in predictions:
        leading = row['predicted'][0]
        for key in model.LABELS:
            by_label[key].update({'acceptable_songs': int(key in row['human']),
                'correct_predictions': int(leading == key and key in row['human']),
                'incorrect_predictions': int(leading == key and key not in row['human'])})
    correct = sum(row['predicted'][0] in row['human'] for row in predictions)
    return {'songs': len(predictions), 'accuracy': correct / len(predictions) if predictions else None,
            'correct': correct, 'incorrect': len(predictions) - correct, 'top_in_human': correct,
            'disagreement_classes': dict(Counter(row['disagreement']['class'] for row in predictions)),
            'maximum_score_gap': max((row['disagreement']['score_gap'] for row in predictions), default=None),
            'per_label': {key: dict(counts) for key, counts in by_label.items()}}


def evaluate(examples, *, feature_set='combined'):
    if len(examples) < 2 or len({row['video_id'] for row in examples}) != len(examples):
        raise ValueError('Evaluation requires at least two distinct songs and one answer per song')
    results = {}
    for candidate, parameters in CANDIDATES.items():
        predictions = []
        for held_out in examples:
            training = [row for row in examples if row['video_id'] != held_out['video_id']]
            scores = held_out['baseline']
            trained_labels = []
            if parameters:
                learned = fit(training, **parameters, feature_set=feature_set)
                scores = model.scores(learned, example_vector(held_out, learned['features']), scores)
                trained_labels = sorted(learned['classifiers'])
            predictions.append({'video_id': held_out['video_id'], 'title': held_out['title'],
                                 'human': held_out['labels'], 'predicted': select(scores),
                                 'disagreement': disagreement(scores, held_out['labels']),
                                 'training_songs': len(training), 'trained_labels': trained_labels})
        results[candidate] = {**metrics(predictions), 'predictions': predictions}
    return results


def divergence_review(examples, results, audio_only):
    """Review frozen errors and candidate performance on the same severe cases.

    Snapshot-derived cohorts are descriptive development diagnostics only. They
    do not change training weights, features, folds or candidate selection.
    """
    cases = []
    for row in examples:
        feedback = row.get('feedback_prediction')
        if feedback is None:
            continue
        baseline = disagreement(row['baseline'], row['labels'])
        cases.append({'video_id': row['video_id'], 'feedback_run_id': row['feedback_run_id'], 'title': row['title'],
                      'artist': row.get('artist', ''), 'human': row['labels'],
                      'feedback_prediction': feedback, 'heuristic': baseline,
                      'learned_reversal': (feedback['model'] is not None
                                           and feedback['class'] == 'wide_divergence'
                                           and baseline['predicted'][0] in row['labels'])})
    cases.sort(key=lambda row: (row['feedback_prediction']['class'] == 'wide_divergence',
                                row['feedback_prediction']['score_gap']), reverse=True)
    wide = {row['video_id'] for row in cases if row['feedback_prediction']['class'] == 'wide_divergence'}

    def cohort(candidates):
        return {name: metrics([row for row in result['predictions'] if row['video_id'] in wide])
                for name, result in candidates.items()}

    return {'definition': {'wide_divergence': 'leading prediction is unacceptable and leading score minus best acceptable score >= gap',
                            'score_gap_threshold': WIDE_DIVERGENCE_GAP, 'scores_are_calibrated': False},
            'feedback_snapshot_songs': len(cases), 'unavailable_feedback_snapshot_songs': len(examples) - len(cases),
            'feedback_classes': dict(Counter(row['feedback_prediction']['class'] for row in cases)),
            'learned_reversals': sum(row['learned_reversal'] for row in cases),
            'cases': cases, 'feedback_wide_divergence_cohort': {
                'songs': len(wide), 'combined': cohort(results), 'audio_only': cohort(audio_only)}}


def train(examples):
    with threadpool_limits(limits=1):
        results = evaluate(examples)
        audio_only = evaluate(examples, feature_set='audio_only')
        best = max(results, key=lambda key: results[key]['accuracy'])
        parameters = CANDIDATES[best]
        learned = fit(examples, **(parameters or {'intercept_only': True}))
        if not parameters:
            learned['classifiers'] = {}
    manifest = [{key: row[key] for key in ('video_id', 'feedback_run_id', 'sample_ids', 'sample_run_ids',
                                         'sample_points', 'sampled_at', 'labels', 'updated_at', 'metadata_sha256', 'subgenre',
                                         'feedback_prediction') if key in row}
                for row in sorted(examples, key=lambda row: row['video_id'])]
    learned['training'].update({'candidate': best, 'data_join': 'track_feedback_and_latest_samples_per_point',
        'dataset_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        'latest_feedback_at': max(row['updated_at'] for row in examples)})
    learned['evaluation'] = {'method': 'development_leave_one_song_out', 'selected_candidate': best,
                             'results': {key: {k: v for k, v in value.items() if k != 'predictions'}
                                         for key, value in results.items()}}
    audio_best = max(audio_only, key=lambda key: audio_only[key]['accuracy'])
    comparison = {'method': 'same_songs_same_folds_same_penalties',
                  'audio_only': {'selected_candidate': audio_best, 'results': audio_only},
                  'combined': {'selected_candidate': best, 'results': results},
                  'accuracy_change': results[best]['accuracy'] - audio_only[audio_best]['accuracy']}
    counts = {name: sum(row['values'][len(model.AUDIO_FEATURES) + i] is not None for row in examples)
              for i, name in enumerate(audio_vibe_metadata.NUMERIC)}
    coverage = {'songs': len(examples), 'observed_numeric_fields': counts,
                'sample_counts': dict(Counter(len(row['sample_ids']) for row in examples)),
                'specific_subgenres': dict(Counter(row['subgenre'] for row in examples if row.get('subgenre'))),
                'songs_with_provider_audio': sum(any(value is not None for value in
                    row['values'][len(model.AUDIO_FEATURES):len(model.AUDIO_FEATURES) + 12]) for row in examples)}
    model.validate(learned)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'selected_candidate': best,
              'target_interpretation': 'any_selected_alternative_is_acceptable',
              'primary_metric': 'single_prediction_accuracy',
              'evaluation': learned['evaluation']['method'], 'results': results, 'manifest': manifest,
              'input_comparison': comparison, 'metadata_coverage': coverage,
              'divergence_review': divergence_review(examples, results, audio_only),
              'limitations': ['Candidate selection uses the same development folds; fresh feedback is needed for acceptance.',
                              'One predicted label is correct when it matches any selected acceptable alternative.',
                              'Joint alternative-label losses use regularization for sparse labels.',
                              'Track feedback joins the latest samples per point, independently of playback or feedback-time sample IDs.',
                              'Partial sample coverage is included and reported; missing excerpts are never fabricated.',
                              'Tracks without compatible measurements and tracks with cleared answers are excluded.',
                              'Frozen-disagreement cohorts are descriptive development diagnostics, not independent acceptance.',
                              'No model is activated and no recommendations or stored answers are changed.']}
    return learned, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    if len({path.resolve() for path in (args.db, args.output, args.report)}) != 3:
        parser.error('Database, model and report must be different files')
    try:
        examples, skipped = examples_from_db(args.db)
        learned, report = train(examples)
    except (ValueError, sqlite3.Error) as error:
        parser.error(str(error))
    report['skipped'] = skipped
    args.output.write_text(json.dumps(learned, indent=2, allow_nan=False) + '\n')
    args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'selected_candidate': report['selected_candidate'], 'training_songs': len(examples),
                      'trained_labels': sorted(learned['classifiers']), 'skipped': skipped,
                      'metadata_coverage': report['metadata_coverage'],
                      'audio_only_candidate': report['input_comparison']['audio_only']['selected_candidate'],
                      'accuracy_change': report['input_comparison']['accuracy_change'],
                      'feedback_disagreements': report['divergence_review']['feedback_classes'],
                      'learned_reversals': report['divergence_review']['learned_reversals'],
                      'scores': {key: {k: v[k] for k in ('accuracy', 'correct', 'songs')}
                                 for key, v in report['results'].items()},
                      'model': str(args.output), 'report': str(args.report)}, indent=2))


if __name__ == '__main__':
    main()
