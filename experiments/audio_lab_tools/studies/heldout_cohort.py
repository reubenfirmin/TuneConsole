"""Freeze candidate artifacts, then score the reserved local additions once.

Local metrics supplement native and cross-corpus human evidence. No fitting,
threshold choice, coefficient changes or live database access occur here.
"""
import argparse
import json
from pathlib import Path
import shutil
import warnings

import numpy as np
from threadpoolctl import threadpool_limits

from .. import TOOLS, source_path
from .completion_campaign import SOURCES, STEPS, VARIANTS, stage_outputs
from ..training.external_binary_calibration import metrics
from .external_checkpoint import CORE, RUN
from ..support.external_experiment import OUT, ROOT, digest, save, stamp
from ..evaluation.external_export_check import check as check_export, replay
from ..training.external_training import SEED, validation_groups
from ..evaluation.external_transfer import SNAPSHOT, VIBES, probabilities

WORK = RUN / 'reserved-cohort'


def inference_hashes():
    return {str(path): digest(path) for path in sorted([
        Path(__file__), source_path('local_reserved.py'), *TOOLS.rglob('external_*.py'),
        *TOOLS.rglob('__init__.py'),
        ROOT / 'src/yt_playlist/rec/audio_vibe_external.py'])}


def completed_receipts(state):
    latest = {row['name']: row for row in state['steps']}
    receipts = {}
    for step in STEPS:
        row = latest.get(step, {})
        if row.get('state') != 'complete' or not row.get('outputs'):
            raise ValueError('Missing completed comparison: ' + step)
        if any(not (ROOT / path).is_file() or digest(ROOT / path) != expected
               for path, expected in row['outputs'].items()):
            raise ValueError('Completed comparison outputs changed: ' + step)
        receipts[step] = {'fingerprint': row['fingerprint'], 'outputs': row['outputs']}
    return receipts


def models():
    core = 'checkpoint530__' + '+'.join(CORE)
    result = {'checkpoint': ROOT / 'src/yt_playlist/rec/data/emotify-vibes.json',
              'core_consensus': OUT / f'models/consensus-{core}.json',
              'core_selected_learners': OUT / f'models/consensus-{core}__native-selected_learner__calibrator-selected_learner.json',
              'core_rankings': OUT / f'models/vibe-ranking-{core}.json',
              'core_semantics': OUT / 'models/checkpoint530-semantic.json',
              'projection': OUT / 'models/gold-vibes.unlabeled-projection.json',
              'projection_rankings': OUT / 'models/gold-vibes.unlabeled-projection-ranking.json'}
    for method in ('ordinary_ridge', 'unlabeled_consistency'):
        result['consistency_' + method] = OUT / f'unlabeled-consistency-deduplicated/models/gold-vibes.{method}.json'
    result['local_pmemo_ablations'] = RUN / 'models/local-ablation/pmemo.reserved-semantics.json'
    for variant in (*VARIANTS, 'expanded'):
        result['consensus_' + variant] = stage_outputs('consensus:' + variant)[-1]
    result['expanded_rankings'] = stage_outputs('ranking:expanded')[-1]
    result['expanded_semantics'] = stage_outputs('semantics:expanded')[-1]
    for variant in ('supervised_shared', 'unlabeled_shared'):
        for family in ('shared_native_qualities', 'embedding_shared_qualities'):
            for kind in ('gold', 'ranking'):
                result[f'{variant}_{kind}_{family}'] = OUT / f'models/multitask/multitask-{variant}.{kind}-{family}.json'
    return result


def verify_frozen_inputs():
    manifest = json.loads((RUN / 'inputs/manifest.json').read_text())
    path = RUN / 'inputs/human-labels.json'
    if digest(path) != manifest['files']['human-labels.json']:
        raise ValueError('Reserved human snapshot changed')
    return json.loads(path.read_text())


def freeze():
    destination = WORK / 'plan.json'
    if destination.exists():
        raise ValueError('Reserved evaluation is already frozen; reuse its exact artifacts')
    human = verify_frozen_inputs()
    completion = json.loads((RUN / 'completion/status.json').read_text())
    if completion['state'] != 'complete' or completion.get('active'):
        raise ValueError('Complete the source comparison campaign before opening the reserved evaluation')
    receipts = completed_receipts(completion)
    ids = set(human['new_label_cohort_ids'])
    if len(ids) != 20:
        raise ValueError('The reserved cohort differs from the agreed 20 additions')
    paths = models()
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise ValueError('Finish and freeze every candidate first: ' + ', '.join(missing))
    candidates = {}
    for name, path in paths.items():
        artifact = json.loads(path.read_text())
        training = set(artifact.get('gold_training_ids', [])) | set(artifact.get('training_ids', []))
        if training & ids:
            raise ValueError('Candidate fitted on reserved labels: ' + name)
        key = digest(path)
        if name == 'checkpoint' and key != '5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0':
            raise ValueError('The agreed product checkpoint changed')
        copied = WORK / 'models' / (key + '.json')
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copied)
        extra = {}
        variant = artifact.get('shared_model_variant')
        if variant:
            parent = OUT / 'models/multitask' / (variant + '.json')
            parent_copy = WORK / 'models' / (digest(parent) + '.json')
            shutil.copyfile(parent, parent_copy)
            extra['shared_model'] = {'path': str(parent_copy), 'sha256': digest(parent_copy)}
        report_path = OUT / (path.stem + '.report.json')
        if artifact.get('report_sha256'):
            if digest(report_path) != artifact['report_sha256']:
                raise ValueError('Candidate no longer matches its completed report: ' + name)
            report = json.loads(report_path.read_text())
            if report.get('additional_protected_identity_rows') != 76:
                raise ValueError('Candidate did not protect the complete local identity cohort: ' + name)
        candidates[name] = {'path': str(copied), 'sha256': key, 'original_path': str(path), **extra}
    signatures = {row['embedding_signature'] for row in human['gold']}
    if len(signatures) != 1:
        raise ValueError('Reserved embeddings do not have one compatible representation')
    historical = json.loads(SNAPSHOT.read_text())
    probe = next(row['embedding_values'] for row in historical['gold']
                 if len(row.get('embedding_values', [])) == 200)
    preflight = {}
    for name, candidate in candidates.items():
        shared = (json.loads(Path(candidate['shared_model']['path']).read_text())
                  if 'shared_model' in candidate else None)
        values = candidate_predictions(json.loads(Path(candidate['path']).read_text()),
                                      np.asarray([probe]), shared_model=shared)
        if not values or any(value.shape != (1, 6) or not np.isfinite(value).all()
                             or (value < 0).any() or (value > 1 + 1e-10).any()
                             for value in values.values()):
            raise ValueError('Candidate fails historical-input preflight: ' + name)
        preflight[name] = list(values)
    plan = {'created_at': stamp(), 'script_sha256': digest(__file__), 'inference_code_sha256': inference_hashes(),
        'historical_input_preflight': preflight,
        'completed_comparison_receipts': receipts,
        'snapshot_sha256': digest(RUN / 'inputs/human-labels.json'),
        'historical_snapshot_sha256': digest(SNAPSHOT), 'embedding_signature': next(iter(signatures)),
        'reserved_ids': sorted(ids), 'candidates': candidates,
        'source_training_cohorts': {'compatible_experts': SOURCES, 'alternative_pools': VARIANTS},
        'policy': 'Freeze every candidate and all comparisons before revealing reserved labels for scoring. Report every candidate; no model selection or refitting on this cohort. Captured-audio heuristic and the unchanged product checkpoint are explicit comparators.',
        'limitations': ['Only 20 additional developer annotations; some per-vibe positives are sparse.',
            'Two additions appeared in earlier prediction-labeled silver data. Report them separately; do not call all 20 prospective.',
            'The labels come from the same developer, not a representative independent listening panel.',
            'Unknown upstream backbone/teacher training overlap and unresolved artist aliases remain.']}
    save(destination, plan)
    print(json.dumps({'reserved_plan': str(destination), 'candidates': list(candidates), 'reserved_rows': len(ids)}), flush=True)


def candidate_predictions(artifact, x, *, shared_model=None):
    kind = artifact['format']
    if kind == 'audio-vibe-emotify-1':
        from yt_playlist.rec.audio_vibe_external import validate
        validate(artifact)
        native = replay({**artifact['native'], 'medians': artifact['native']['mean']}, x)
        return {'prediction': probabilities(replay({**artifact['calibration'], 'medians': artifact['calibration']['mean']}, native))}
    if kind == 'external-research-consistency-ridge-1':
        return {'prediction': replay(artifact, x)}
    if kind == 'research-local-native-semantics-1':
        from ..evaluation.external_export_check import arrays
        from ..training.external_multitask import prediction
        from ..training.external_semantic import percentiles, semantic_scores
        result = {}
        for name, method in artifact['methods'].items():
            if method['kind'] == 'ridge':
                raw = replay(method['model'], x)
            else:
                model = arrays(method['model'])
                model['parameters'] = arrays(model['parameters'])
                raw = prediction(model, x)[:, :len(artifact['target_names'])]
            ranked = percentiles(np.asarray(method['reference_predictions']), raw)
            result[name] = semantic_scores(ranked, artifact['target_names'], artifact['rules'], artifact['aliases'])
        return result
    if kind in ('external-research-consensus-2', 'external-research-vibe-ranking-1'):
        binary = kind == 'external-research-vibe-ranking-1'
        features = {'embedding': x}
        for source, head in artifact['native_heads'].items():
            features[source] = replay(head['model'], x)[:, head['selected_columns']]
        experts = [replay(artifact['calibrators'][name], features[name]) for name in artifact['expert_names']]
        if not binary:
            experts = [probabilities(value) for value in experts]
        result = {}
        for policy in ('selected_candidate', 'selected_consensus' if binary else 'selected_combination'):
            selected = artifact['combinations'][artifact[policy]]
            result[policy] = sum(experts[index] * np.asarray(selected['weights'])[offset]
                                 for offset, index in enumerate(selected['indices']))
        return result
    if kind == 'external-research-fixed-semantics-1':
        from ..training.external_semantic import predict_export
        return predict_export(artifact, x)
    if kind in ('external-research-unlabeled-projection-1', 'external-research-unlabeled-projection-ranking-1'):
        from ..evaluation.external_export_check import arrays
        from ..training.external_unlabeled_projection import transform
        values = transform(arrays(artifact['projection']), x, artifact['dimensions'], artifact['whitening'])
        return {'prediction': replay(artifact.get('model', artifact.get('calibrator')), values)}
    if kind in ('external-research-multitask-vibes-1', 'external-research-multitask-vibe-ranking-1'):
        from ..evaluation.external_export_check import arrays
        from ..training.external_multitask import representation, prediction
        model = arrays(shared_model['model'])
        model['parameters'] = arrays(model['parameters'])
        features = {'embedding': x, 'shared_hidden': representation(model, x),
                    'shared_native_qualities': prediction(model, x)}
        features['embedding_shared_hidden'] = np.column_stack([x, features['shared_hidden']])
        features['embedding_shared_qualities'] = np.column_stack([x, features['shared_native_qualities']])
        return {'prediction': replay(artifact['calibrator'], features[artifact['family']])}
    raise ValueError('Unsupported frozen candidate format: ' + kind)


def paired_bootstrap(y, baseline, values, groups):
    baseline = probabilities(baseline)
    values = probabilities(values)
    allowed = y > 0
    correct = lambda p: allowed[np.arange(len(y)), p.argmax(1)].astype(float)
    nll = lambda p: -np.log(np.maximum((p * allowed).sum(1), 1e-12))
    changes = {'acceptable_top_choice_gain': correct(values) - correct(baseline),
               'acceptable_nll_reduction': nll(baseline) - nll(values)}
    keys = sorted(set(groups))
    rng = np.random.default_rng(SEED)
    draws = rng.integers(len(keys), size=(2000, len(keys)))
    sizes = np.asarray([sum(group == key for group in groups) for key in keys])
    result = {}
    for name, delta in changes.items():
        sums = np.asarray([delta[np.asarray(groups) == key].sum() for key in keys])
        boot = sums[draws].sum(1) / sizes[draws].sum(1)
        result[name] = {'difference': float(delta.mean()),
                        'conditional_95_percent_interval': np.quantile(boot, [.025, .975]).tolist()}
    return result


def evaluate():
    if (WORK / 'report.json').exists():
        raise ValueError('Reserved cohort has already been evaluated; preserve the first result')
    plan = json.loads((WORK / 'plan.json').read_text())
    if plan['script_sha256'] != digest(__file__):
        raise ValueError('Reserved evaluation code changed after freezing')
    if plan['inference_code_sha256'] != inference_hashes():
        raise ValueError('Reserved inference dependencies changed after freezing')
    if digest(SNAPSHOT) != plan['historical_snapshot_sha256']:
        raise ValueError('Historical training snapshot changed after freezing')
    if digest(RUN / 'inputs/human-labels.json') != plan['snapshot_sha256']:
        raise ValueError('Reserved snapshot changed after freezing')
    human = verify_frozen_inputs()
    by_id = {row['video_id']: row for row in human['gold']}
    rows = [by_id[key] for key in plan['reserved_ids']]
    x = np.asarray([row['embedding_values'] for row in rows])
    if x.shape != (20, 200) or not np.isfinite(x).all() or any(
            row['embedding_signature'] != plan['embedding_signature'] for row in rows):
        raise ValueError('Reserved acoustic inputs changed')
    predictions, checks = {}, {}
    for name, candidate in plan['candidates'].items():
        path = Path(candidate['path'])
        if digest(path) != candidate['sha256']:
            raise ValueError('Frozen candidate changed: ' + name)
        artifact = json.loads(path.read_text())
        independent = None
        if artifact['format'] in ('external-research-consensus-2', 'external-research-vibe-ranking-1'):
            original = Path(candidate['original_path'])
            if digest(original) != candidate['sha256']:
                raise ValueError('Original candidate changed after freezing')
            independent = check_export(original, x)
        shared = None
        if 'shared_model' in candidate:
            parent = candidate['shared_model']
            if digest(parent['path']) != parent['sha256']:
                raise ValueError('Frozen shared representation changed')
            shared = json.loads(Path(parent['path']).read_text())
        values = candidate_predictions(artifact, x, shared_model=shared)
        for method, value in values.items():
            if value.shape != (20, 6) or not np.isfinite(value).all() or (value < 0).any() or (value > 1 + 1e-10).any():
                raise ValueError('Invalid reserved prediction: ' + name)
            predictions[name + ':' + method] = value
        checks[name] = {'sha256': candidate['sha256'], 'predictions': list(values),
                        'independent_export_replay': independent}
    predictions['captured_heuristic'] = np.asarray([[row['baseline'][vibe] for vibe in VIBES] for row in rows])
    # The numerical outputs above are now fixed. Human labels enter only here.
    y = np.asarray([[int(vibe in row['labels']) for vibe in VIBES] for row in rows])
    groups = validation_groups([{**row, 'id': row['video_id'], 'group': row['video_id']} for row in rows])
    prior_silver = set(human['new_labels_previously_in_silver'])
    cohorts = {'all_reserved': np.ones(20, dtype=bool),
               'no_known_prior_silver': np.asarray([row['video_id'] not in prior_silver for row in rows]),
               'prior_silver': np.asarray([row['video_id'] in prior_silver for row in rows])}
    baseline = predictions['checkpoint:prediction']
    result = {'created_at': stamp(), 'plan_sha256': digest(WORK / 'plan.json'), 'candidates': checks,
              'cohorts': {}, 'limitations': plan['limitations'], 'model_selected_on_reserved_labels': False}
    for cohort, retained in cohorts.items():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            methods = {name: metrics(y[retained], values[retained]) for name, values in predictions.items()}
        # Two prior-silver examples can lack both classes for every vibe.
        for method in methods.values():
            if not np.isfinite(method['macro_roc_auc']):
                method['macro_roc_auc'] = None
        for name, values in predictions.items():
            methods[name]['versus_checkpoint'] = paired_bootstrap(y[retained], baseline[retained], values[retained],
                np.asarray(groups)[retained].tolist())
        result['cohorts'][cohort] = {'rows': int(retained.sum()), 'methods': methods}
    save(WORK / 'predictions.json', {'ids': plan['reserved_ids'], 'observed': y.tolist(), 'groups': groups,
         'predictions': {name: value.tolist() for name, value in predictions.items()}})
    save(WORK / 'report.json', result)
    print(json.dumps({'reserved_evaluation': str(WORK / 'report.json'), 'cohorts': {
        cohort: {'rows': item['rows'], 'methods': {name: {'macro_ap': metrics['macro_average_precision'],
            'top_choice': metrics['top_choice']} for name, metrics in item['methods'].items()}}
        for cohort, item in result['cohorts'].items()}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'evaluate'])
    args = parser.parse_args()
    if args.action == 'freeze':
        freeze()
    else:
        with threadpool_limits(limits=1):
            evaluate()


if __name__ == '__main__':
    main()
