"""Export native-selected local-audio ablations before reserved label scoring."""
import argparse
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from . import external_local_ablation as ablation
from .external_checkpoint import RUN, protected_identities
from .external_experiment import OUT, digest, save, stamp
from .external_learners import serializable
from .external_semantic import VA_RULES
from .external_training import ALPHAS, SEED, feature_matrices, folds, predict, validation_groups
from .external_transfer import SNAPSHOT, VIBES, excluded_identities

DESTINATION = RUN / 'models/local-ablation/pmemo.reserved-semantics.json'


def build():
    if DESTINATION.exists():
        raise ValueError('Local reserved candidate is already fixed; do not replace it')
    source = OUT / 'pmemo.features.json'
    index = json.loads(source.read_text())['rows']
    protected = protected_identities()
    excluded = {row['id'] for row in excluded_identities(index, protected)}
    rows = [row for row in index if row['id'] not in excluded]
    names = rows[0]['target_names']
    x = feature_matrices(rows)[0]['embedding']
    y = np.asarray([row['targets'] for row in rows])
    groups = np.asarray(validation_groups(rows))
    pool_path = RUN / 'local-audio-pool.json'
    pool = json.loads(pool_path.read_text())
    manifest = RUN / 'inputs/manifest.json'
    inputs = json.loads(manifest.read_text())
    library_path = RUN / 'inputs/library.json'
    if pool['input_manifest_sha256'] != digest(manifest) or digest(library_path) != inputs['files']['library.json']:
        raise ValueError('Frozen local inputs changed')
    library = json.loads(library_path.read_text())
    historical = json.loads(SNAPSHOT.read_text())
    # Use the full native cohort for conservative pool/contributor exclusion,
    # even though protected local identities were removed from native fitting.
    local_rows, teacher_rows, rejected = ablation.disjoint_pool('pmemo', index, pool['rows'], library, historical)
    if not local_rows or not teacher_rows:
        raise ValueError('Local native/teacher comparisons require eligible acoustic inputs')
    local = np.asarray([row['embedding_values'] for row in local_rows])
    teacher_x = np.asarray([row['embedding_values'] for row in teacher_rows])
    teacher_y = np.asarray([row['teacher']['scores'] for row in teacher_rows])
    if any(row['embedding_signature'] != pool['rows'][0]['embedding_signature'] for row in local_rows):
        raise ValueError('Mixed local acoustic signatures')
    from yt_playlist.core.audio_embedding import signature
    example = json.loads(Path(rows[0]['feature_file']).read_text())['prediction']
    native_signature = signature({**example, 'artifact_hashes': {
        item['path']: item['sha256'] for item in example['bundle']['files']}})
    if any(row['embedding_signature'] != native_signature for row in local_rows):
        raise ValueError('Local and native acoustic provenance differ')
    pinned = (source, manifest, library_path, pool_path, SNAPSHOT,
              *(Path(row['feature_file']) for row in rows))
    input_hashes = {str(path): digest(path) for path in pinned}
    split = folds(groups.tolist(), count=3, seed=SEED + 1)
    graph_losses = dict.fromkeys([(strength, alpha) for strength in ablation.STRENGTHS for alpha in ALPHAS], 0.)
    soft_losses = dict.fromkeys(graph_losses, 0.)
    shared_losses = dict.fromkeys([('none', 0., penalty) for penalty in ablation.PENALTIES] +
        [('normalization', 0., penalty) for penalty in ablation.PENALTIES] +
        [('teacher', strength, penalty) for strength in ablation.STRENGTHS if strength for penalty in ablation.PENALTIES], 0.)
    for fold in range(3):
        train, valid = split != fold, split == fold
        for key, model in ablation.graph_candidates(x[train], y[train], local).items():
            graph_losses[key] += float(np.abs(predict(model, x[valid]) - y[valid]).sum())
        targets = ablation.external_soft_targets(x[train], y[train], local)
        for key, model in ablation.distillation_candidates(x[train], y[train], local, targets).items():
            soft_losses[key] += float(np.abs(predict(model, x[valid]) - y[valid]).sum())
        for mode, strength, penalty in shared_losses:
            model = ablation.shared_model(x[train], y[train], teacher_x, teacher_y, strength, penalty,
                normalize_only=mode == 'normalization')
            values = ablation.shared_predict(model, x[valid])[:, :len(names)]
            shared_losses[mode, strength, penalty] += float(np.abs(values - y[valid]).sum())
        print(json.dumps({'local_reserved_export': 'pmemo', 'native_inner_fold': fold}), flush=True)
    selected = {
        'linear_no_local': min((key for key in graph_losses if key[0] == 0), key=graph_losses.get),
        'local_neighbourhood_selected': min(graph_losses, key=graph_losses.get),
        'external_soft_target_selected': min(soft_losses, key=soft_losses.get),
        'shared_no_local': min((key for key in shared_losses if key[0] == 'none'), key=shared_losses.get),
        'shared_local_normalization': min((key for key in shared_losses if key[0] == 'normalization'), key=shared_losses.get),
        'stored_teacher_selected': min(shared_losses, key=shared_losses.get)}
    graph = ablation.graph_candidates(x, y, local)
    soft = ablation.distillation_candidates(x, y, local, ablation.external_soft_targets(x, y, local))
    methods = {}
    for method, key in selected.items():
        if method in ('linear_no_local', 'local_neighbourhood_selected'):
            model, kind = graph[key], 'ridge'
            reference = predict(model, x)
        elif method == 'external_soft_target_selected':
            model, kind = soft[key], 'ridge'
            reference = predict(model, x)
        else:
            mode, strength, penalty = key
            model = ablation.shared_model(x, y, teacher_x, teacher_y, strength, penalty,
                normalize_only=mode == 'normalization')
            kind = 'shared'
            reference = ablation.shared_predict(model, x)[:, :len(names)]
        methods[method] = {'kind': kind, 'selected_parameters': list(key), 'model': serializable(model),
                           'reference_predictions': reference.tolist()}
    teachers = sorted({video for row in teacher_rows
                       for video in row['teacher']['training_provenance']['local_training_video_ids']})
    artifact = {'format': 'research-local-native-semantics-1', 'created_at': stamp(), 'vibes': VIBES,
        'source': 'pmemo', 'target_names': names, 'rules': VA_RULES, 'aliases': {}, 'methods': methods,
        'training_ids': [row['id'] for row in rows], 'gold_training_ids': teachers,
        'excluded_native_ids': sorted(excluded), 'additional_protected_identity_rows': len(protected),
        'embedding_signature': local_rows[0]['embedding_signature'],
        'local_input_recordings': [row['recording_id'] for row in local_rows],
        'teacher_input_recordings': [row['recording_id'] for row in teacher_rows], 'local_exclusions': rejected,
        'input_sha256': input_hashes,
        'script_sha256': digest(__file__), 'ablation_script_sha256': digest(ablation.__file__),
        'policy': 'Three grouped native training folds select regularization and local weight by PMEmo human-target MAE. All 76 local protected identities are excluded from native fitting. Fixed native percentile/VA semantics map all methods to the unchanged six vibes without local-label calibration. Baselines and all ablations are retained.',
        'limitations': ['A small local acoustic pool and one fixed optimization seed; no large-pool conclusion.',
            'Stored teachers used earlier developer labels, declared as fit dependencies; they are not independent truth.',
            'Rules are approximate native-emotion proxies, especially for Focus; percentile scores are not calibrated probabilities.',
            'Native-held-out performance is reported by the earlier local ablation; this artifact is a full protected-cohort refit for a separate reserved check.',
            'Upstream training overlap and unresolved artist aliases remain.']}
    if any(digest(path) != expected for path, expected in input_hashes.items()):
        raise ValueError('Local reserved fit inputs changed during training')
    save(DESTINATION, artifact)
    print(json.dumps({'local_reserved_candidate': str(DESTINATION), 'native_rows': len(rows), 'selected': selected}), flush=True)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    with threadpool_limits(limits=1):
        build()


if __name__ == '__main__':
    main()
