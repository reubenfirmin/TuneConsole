"""Test twelve real local audio inputs against held-out external human targets.

Local scores stay soft teacher outputs. Native validation selects their weight,
including zero; the twenty newer local human labels remain unopened for scoring.
"""
from collections import Counter
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from ..studies.external_checkpoint import RUN
from ..training.external_consistency import fit_path as graph_path
from ..support.external_experiment import OUT, digest, save, stamp
from ..training.external_learners import distance, serializable
from ..training.external_multitask import fit as shared_fit, prediction as shared_predict
from ..datasets.external_secondary import artist_keys, identity
from ..training.external_training import (ALPHAS, SEED, bootstrap_improvement, feature_matrices,
                                        folds, metrics, predict, standardize, validation_groups)
from ..evaluation.external_transfer import SNAPSHOT, VIBES

STRENGTHS = (0., .03, .1, .3)
PENALTIES = (.0001, .001)


def disjoint_pool(source, target_rows, pool, library, historical):
    artists = {name for row in target_rows for name in artist_keys(row.get('artist', ''))}
    titles = {identity(row['title']) for row in target_rows if row.get('title')}
    recordings = {row['id']: row for row in library['recordings']}
    training = {row['video_id']: row for row in historical['gold']}

    def overlaps(row):
        return bool(artist_keys(row.get('artist', '')) & artists or
                    (identity(row.get('title', '')) in titles if row.get('title') else False))

    native_family = 'emotify' if source.startswith('emotify') else source.removesuffix('-dynamic')
    selected, teachers, rejected = [], [], []
    for row in pool:
        if overlaps(row):
            rejected.append({'recording_id': row['recording_id'], 'reason': 'target_artist_or_title'})
            continue
        selected.append(row)
        teacher = row['teacher']
        provenance = teacher['training_provenance']
        reason = None
        if not teacher['lineage_complete'] or teacher['scores'] is None:
            reason = 'incomplete_teacher_lineage'
        elif not provenance['known_fit_dependencies_complete']:
            reason = 'unknown_teacher_fit'
        elif native_family in provenance['native_training_corpora']:
            reason = 'teacher_fitted_on_target_corpus'
        elif any(overlaps(recordings[key]) for key in teacher['contributor_recording_ids']):
            reason = 'target_artist_or_title_in_teacher_contributors'
        elif any(key not in training or overlaps(training[key])
                 for key in provenance['local_training_video_ids']):
            reason = 'target_overlap_or_missing_identity_in_teacher_local_training'
        if reason:
            rejected.append({'recording_id': row['recording_id'], 'teacher_only': True, 'reason': reason})
        else:
            teachers.append(row)
    return selected, teachers, rejected


def graph_differences(x, local):
    """Local-to-training neighbours are a smoothness assumption, not invariance."""
    _, _, mean, scale = standardize(x)
    neighbours = np.argsort(distance((local - mean) / scale, (x - mean) / scale), axis=1)[:, :3]
    return (local[:, None, :] - x[neighbours]).reshape(-1, x.shape[1])


def shared_model(x, y, local, teacher, strength, penalty, *, normalize_only=False):
    width = y.shape[1]
    # Keep output dimensions and initialization paired with the zero-weight
    # model; unused auxiliary columns have zero weights and no training loss.
    targets = np.column_stack([y, np.zeros((len(y), len(VIBES)))])
    rows = [{'task': 'native'} for _ in x]
    offsets, weights = {'native': (0, width)}, {'native': 1.}
    if normalize_only:
        x = np.vstack([x, local])
        targets = np.vstack([targets, np.zeros((len(local), width + len(VIBES)))])
        rows += [{'task': 'native', 'native_label_withheld': True} for _ in local]
    if strength:
        x = np.vstack([x, local])
        targets = np.vstack([targets, np.column_stack([np.zeros((len(local), width)), teacher])])
        rows += [{'task': 'stored_teacher'} for _ in local]
        offsets['stored_teacher'] = (width, width + len(VIBES))
        weights['stored_teacher'] = strength
    return shared_fit(x, targets, rows, offsets, 32, penalty, 0., task_weights=weights)


def graph_candidates(x, y, local):
    differences = graph_differences(x, local)
    return {(strength, model['alpha']): model
            for strength in STRENGTHS for model in graph_path(x, y, differences, [strength])}


def external_soft_targets(x, y, local):
    """A fixed native RBF teacher uses only this partition's human targets."""
    z, medians, mean, scale = standardize(x)
    kernel = np.exp(-.5 * distance(z, z) / z.shape[1])
    column_mean, grand_mean = kernel.mean(0), float(kernel.mean())
    centered = kernel - kernel.mean(1, keepdims=True) - column_mean + grand_mean
    intercept = y.mean(0)
    dual = np.linalg.solve(centered + .1 * np.eye(len(x)), y - intercept)
    reference = np.exp(-.5 * distance((local - mean) / scale, z) / z.shape[1])
    values = intercept + (reference - reference.mean(1, keepdims=True) - column_mean + grand_mean) @ dual
    return np.clip(values, 0, 1)


def distillation_candidates(x, y, local, targets):
    """Weighted native ridge; preprocessing is fitted on native training only."""
    z, medians, mean, scale = standardize(x)
    extra = (local - mean) / scale
    candidates = {}
    for strength in STRENGTHS:
        # This is total auxiliary weight relative to native weight, independent
        # of how many captures or song sections the local pool contains.
        weight = np.concatenate([np.ones(len(x)), np.full(len(local), strength * len(x) / len(local))])
        features, observed = np.vstack([z, extra]), np.vstack([y, targets])
        center = np.average(features, axis=0, weights=weight)
        target_mean = np.average(observed, axis=0, weights=weight)
        root = np.sqrt(weight)[:, None]
        u, singular, vt = np.linalg.svd((features - center) * root, full_matrices=False)
        projected = u.T @ ((observed - target_mean) * root)
        for alpha in ALPHAS:
            coefficients = (vt.T * (singular / (singular ** 2 + alpha))) @ projected
            candidates[strength, alpha] = {'medians': medians, 'mean': mean, 'scale': scale,
                'weights': coefficients, 'intercept': target_mean - center @ coefficients,
                'alpha': alpha, 'local_distillation_strength': strength}
    return candidates


def evaluate(source='soundtracks-all'):
    with threadpool_limits(limits=1):
        return _evaluate(source)


def _evaluate(source):
    path = OUT / f'{source}.features.json'
    rows = json.loads(path.read_text())['rows']
    names = rows[0]['target_names']
    if any(row['target_names'] != names for row in rows):
        raise ValueError('Native target order changed')
    x = feature_matrices(rows)[0]['embedding']
    y = np.asarray([row['targets'] for row in rows], dtype=float)
    groups = np.asarray(validation_groups(rows))
    pool_path = RUN / 'local-audio-pool.json'
    pool_export = json.loads(pool_path.read_text())
    manifest_path = RUN / 'inputs/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if pool_export['input_manifest_sha256'] != digest(manifest_path):
        raise ValueError('Local audio pool and frozen inputs differ')
    if digest(RUN / 'inputs/library.json') != manifest['files']['library.json']:
        raise ValueError('Frozen teacher lineage changed')
    pool = pool_export['rows']
    from yt_playlist.core.audio_embedding import signature
    example = json.loads(Path(rows[0]['feature_file']).read_text())['prediction']
    native_signature = signature({**example, 'artifact_hashes': {
        item['path']: item['sha256'] for item in example['bundle']['files']}})
    if any(row['embedding_signature'] != native_signature for row in pool):
        raise ValueError('Local and external acoustic model provenance differs')
    library = json.loads((RUN / 'inputs/library.json').read_text())
    historical = json.loads(SNAPSHOT.read_text())
    local_rows, teacher_rows, rejected = disjoint_pool(source, rows, pool, library, historical)
    if not local_rows:
        raise ValueError('No disjoint local acoustic inputs')
    local = np.asarray([row['embedding_values'] for row in local_rows])
    teacher_x = np.asarray([row['embedding_values'] for row in teacher_rows])
    teacher_y = np.asarray([row['teacher']['scores'] for row in teacher_rows])
    split = folds(groups.tolist())
    predictions = {name: np.zeros_like(y) for name in ('linear_no_local', 'local_neighbourhood_selected',
        'external_soft_target_selected', 'shared_no_local', 'shared_local_normalization', 'stored_teacher_selected')}
    chosen = []
    for fold in range(5):
        train, valid = split != fold, split == fold
        inner = folds(groups[train].tolist(), count=3, seed=SEED + 1)
        inner_x, inner_y = x[train], y[train]
        graph_losses = dict.fromkeys([(s, a) for s in STRENGTHS for a in ALPHAS], 0.)
        distillation_losses = dict.fromkeys(graph_losses, 0.)
        shared_keys = [('none', 0., p) for p in PENALTIES]
        if teacher_rows:
            shared_keys += [('normalization', 0., p) for p in PENALTIES]
            shared_keys += [('teacher', s, p) for s in STRENGTHS if s for p in PENALTIES]
        shared_losses = dict.fromkeys(shared_keys, 0.)
        for part in range(3):
            fit, test = inner != part, inner == part
            for key, model in graph_candidates(inner_x[fit], inner_y[fit], local).items():
                graph_losses[key] += float(np.abs(predict(model, inner_x[test]) - inner_y[test]).sum())
            soft_targets = external_soft_targets(inner_x[fit], inner_y[fit], local)
            for key, model in distillation_candidates(inner_x[fit], inner_y[fit], local, soft_targets).items():
                distillation_losses[key] += float(np.abs(predict(model, inner_x[test]) - inner_y[test]).sum())
            for (mode, strength, penalty) in shared_losses:
                model = shared_model(inner_x[fit], inner_y[fit], teacher_x, teacher_y, strength, penalty,
                                     normalize_only=mode == 'normalization')
                values = shared_predict(model, inner_x[test])[:, :len(names)]
                shared_losses[mode, strength, penalty] += float(np.abs(values - inner_y[test]).sum())
        selections = {
            'linear_no_local': min((key for key in graph_losses if key[0] == 0), key=graph_losses.get),
            'local_neighbourhood_selected': min(graph_losses, key=graph_losses.get),
            'external_soft_target_selected': min(distillation_losses, key=distillation_losses.get),
            'shared_no_local': min((key for key in shared_losses if key[0] == 'none'), key=shared_losses.get),
            'shared_local_normalization': min((key for key in shared_losses
                if key[0] == ('normalization' if teacher_rows else 'none')), key=shared_losses.get),
            'stored_teacher_selected': min(shared_losses, key=shared_losses.get)}
        fitted_graph = graph_candidates(x[train], y[train], local)
        fitted_distillation = distillation_candidates(x[train], y[train], local,
                                                      external_soft_targets(x[train], y[train], local))
        shared_cache = {}
        for method, key in selections.items():
            if method in ('linear_no_local', 'local_neighbourhood_selected'):
                model = fitted_graph[key]
                predictions[method][valid] = predict(model, x[valid])
            elif method == 'external_soft_target_selected':
                model = fitted_distillation[key]
                predictions[method][valid] = predict(model, x[valid])
            else:
                if key not in shared_cache:
                    mode, strength, penalty = key
                    shared_cache[key] = shared_model(x[train], y[train], teacher_x, teacher_y,
                        strength, penalty, normalize_only=mode == 'normalization')
                model = shared_cache[key]
                predictions[method][valid] = shared_predict(model, x[valid])[:, :len(names)]
            save(RUN / 'models/local-ablation' / f'{source}.{method}.fold{fold}.json', {
                'format': 'research-local-ablation-fold-1', 'source': source, 'method': method,
                'target_names': names, 'outer_fold': fold,
                'training_ids': [row['id'] for row, keep in zip(rows, train) if keep],
                'selected_parameters': list(key), 'model': serializable(model)})
        chosen.append({'fold': fold, 'methods': {method: list(key) for method, key in selections.items()}})
        print(json.dumps({'local_ablation': source, 'outer_fold': fold, 'selected': chosen[-1]['methods']}), flush=True)
    results = {name: metrics(y, values, names) for name, values in predictions.items()}
    for method, baseline in [('local_neighbourhood_selected', 'linear_no_local'),
                             ('external_soft_target_selected', 'linear_no_local'),
                             ('stored_teacher_selected', 'shared_no_local')]:
        results[method]['versus_zero_local_weight'] = bootstrap_improvement(
            y, predictions[baseline], predictions[method], groups.tolist(), list(range(len(names))))
    results['stored_teacher_selected']['versus_local_normalization_control'] = bootstrap_improvement(
        y, predictions['shared_local_normalization'], predictions['stored_teacher_selected'],
        groups.tolist(), list(range(len(names))))
    report = {'created_at': stamp(), 'dataset': source, 'rows': len(rows),
        'native_source_sha256': digest(path), 'local_pool_sha256': digest(pool_path),
        'script_sha256': digest(__file__), 'shared_trainer_sha256': digest(Path(shared_fit.__code__.co_filename)),
        'local_input_recordings': [row['recording_id'] for row in local_rows],
        'stored_teacher_recordings': [row['recording_id'] for row in teacher_rows],
        'local_exclusions': rejected, 'exclusions_by_reason': dict(Counter(row['reason'] for row in rejected)),
        'methods': results, 'choices_by_fold': chosen,
        'grid': {'local_strength': STRENGTHS, 'ridge_alpha': ALPHAS,
                 'shared_hidden': 32, 'shared_penalty': PENALTIES, 'shared_steps': 400},
        'external_soft_teacher': {'kind': 'native RBF ridge', 'gamma': .5, 'alpha': .1,
            'policy': 'Fixed before this ablation; refit using only each inner/outer training partition. No stored local scores.'},
        'validation': 'Five native group outer folds; three inner folds select penalty and local weight by external human-target MAE. Local artist/title identities are excluded against the entire target corpus. Stored teachers additionally exclude target-dependent fits and contributors. New local human labels are never scored or used for selection.',
        'limitations': ['Only twelve local audio inputs are available before exclusions; no large-pool conclusion is possible.',
                       'The neighbourhood penalty assumes nearby different recordings have similar native targets; it is not same-excerpt augmentation or section invariance.',
                       'Stored teacher outputs are an auxiliary six-output task, not independent native emotion labels or extra measurement votes.',
                       'The local-normalization control uses exactly the eligible teacher audio inputs without their soft targets.',
                       'Both local objectives can select zero weight; their baselines retain identical native folds and model families.',
                       'Unknown upstream audio overlap and artist aliases remain unresolved.',
                       'This pilot uses one fixed random seed; shared-model optimization variability is not included in the paired bootstrap.',
                       'Verified repeat-window objectives are unavailable without establishing that repeat captures contain the same audio window. Different song sections are not forced to agree.']}
    save(RUN / f'{source}.local-ablation.report.json', report)
    save(RUN / f'{source}.local-ablation.oof.json', {'ids': [row['id'] for row in rows],
        'folds': split.tolist(), 'observed': y.tolist(),
        'predictions': {name: value.tolist() for name, value in predictions.items()}})
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', default='soundtracks-all')
    options = parser.parse_args()
    with threadpool_limits(limits=1):
        evaluate(options.dataset)
