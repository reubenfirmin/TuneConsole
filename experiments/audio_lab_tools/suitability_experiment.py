"""Freeze, fit and evaluate the #572 score contract without product changes."""
import argparse
import json
from pathlib import Path
import shutil

import numpy as np
from threadpoolctl import threadpool_limits

from . import cruise_specialist as specialist
from . import suitability_calibration as calibration
from .vibe_score_contract import coverage
from .external_experiment import LAB, digest, save, stamp
from .mapped_vibe_inference import replay
from .mapped_vibe_metrics import discounted_gain, summary, top_count
from .transfer_identity import exclusion
from .vibe_specialist_round import EXPERIMENTS, input_arrays, inside, load, npz, versions

ROUND = LAB / 'local/mapped-vibes-20261029'
DEFAULT = ROUND / 'score-contract-1'
SOURCE = 'manymusic-natural'
TRANSFERS = ('manymusic-generated', 'memo2496-pilot')
CONTROLS = ('checkpoint', 'refresh_calibration', 'refresh_both')
PROTOCOL = LAB / 'VIBE_SCORE_CONTRACT.md'


def code_paths():
    names = ('suitability_experiment.py', 'suitability_calibration.py', 'vibe_score_contract.py',
             'test_suitability_contract.py', 'cruise_specialist.py', 'external_experiment.py',
             'external_training.py', 'mapped_vibe_inference.py', 'mapped_vibe_metrics.py',
             'mapped_vibe_rules.py', 'transfer_identity.py', 'external_secondary.py',
             'vibe_specialist_round.py')
    paths = {f'audio_lab_tools/{name}': Path(__file__).with_name(name) for name in names}
    paths.update({'run.py': EXPERIMENTS / 'run.py', 'audio_lab/VIBE_SCORE_CONTRACT.md': PROTOCOL,
                  'product/audio_vibe_external.py': Path(specialist.shared.__file__),
                  'product/audio_embedding.py': Path(specialist.audio_embedding.__file__),
                  'product/build_runtime.py': EXPERIMENTS.parent / 'scripts/audio_model/build_runtime.py'})
    return paths


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = digest(source)
    shutil.copyfile(source, destination)
    if digest(destination) != expected:
        raise ValueError('Input changed during freeze: ' + str(source))
    return expected


def freeze(directory):
    if directory.exists():
        raise ValueError('Preserve previous rounds; choose a new directory')
    baseline = ROUND / 'cruise-baselines-1'
    receipt = load(baseline / 'evaluation.receipt.json')
    if receipt['plan_sha256'] != digest(baseline / 'plan.json'):
        raise ValueError('Baseline plan differs from evaluation receipt')
    for relative, expected in receipt['outputs'].items():
        if digest(baseline / relative) != expected:
            raise ValueError('Baseline evidence changed')
    fit_receipt = load(baseline / 'fit.receipt.json')
    if receipt['fit_receipt_sha256'] != digest(baseline / 'fit.receipt.json'):
        raise ValueError('Baseline fit receipt changed')
    baseline_plan = load(baseline / 'plan.json')
    if fit_receipt['plan_sha256'] != digest(baseline / 'plan.json'):
        raise ValueError('Baseline fit plan changed')
    references = {}
    for name in ('external-predictions.npz', 'evaluation.report.json', 'evaluation.receipt.json',
                 'fit.receipt.json', 'plan.json', 'checkpoint.json', 'refresh_calibration.json',
                 'refresh_both.json', 'cruise-specialist.json'):
        path = baseline / name
        expected = fit_receipt['outputs'].get(name, baseline_plan['artifacts'].get(name))
        if expected is not None and digest(path) != expected:
            raise ValueError('Baseline artifact changed: ' + name)
        references[str(path)] = copy(path, directory / 'inputs/baseline' / name)
    candidate = ROUND / 'common-target-1'
    candidate_plan = load(candidate / 'plan.json')
    for target in (SOURCE, *TRANSFERS):
        for suffix in ('json', 'npz'):
            name = f'inputs/{target}.{suffix}'
            path = candidate / name
            if digest(path) != candidate_plan['inputs'][name]:
                raise ValueError('Pilot input differs from frozen candidate round')
            references[str(path)] = copy(path, directory / name)
    model = specialist.load(directory / 'inputs/baseline/cruise-specialist.json')
    if model['header']['embedding_signature'] != candidate_plan['embedding_signature']:
        raise ValueError('Pilot and compact expert embedding signatures differ')
    for name in CONTROLS:
        shared = specialist.shared.validate(load(directory / f'inputs/baseline/{name}.json'))
        if shared['embedding_signature'] != candidate_plan['embedding_signature']:
            raise ValueError('Shared control embedding signature differs')
    audits = {}
    for source in specialist.SOURCES:
        for name in (f'{source}.json', f'selection-pilots__{source}.npz'):
            path = candidate / 'inputs' / name
            if digest(path) != candidate_plan['inputs']['inputs/' + name]:
                raise ValueError('Native fitting cohort changed')
            references[str(path)] = copy(path, directory / 'inputs/native' / name)
        path = candidate / f'pilots-{source}.experts.json'
        if digest(path) != model['experts'][source]['source_expert_sha256']:
            raise ValueError('Compact expert provenance differs')
        references[str(path)] = copy(path, directory / 'inputs/native' / path.name)
        native = load(directory / f'inputs/native/{source}.json')
        keep = input_arrays(directory / f'inputs/native/selection-pilots__{source}.npz')['retained']
        fitted = [r for r, selected in zip(native['rows'], keep, strict=True) if selected]
        audits[source] = {}
        for target in (SOURCE, *TRANSFERS):
            metadata = load(directory / f'inputs/{target}.json')
            retained, audit = exclusion(metadata['rows'], fitted)
            audits[source][target] = audit
            if not retained.all():
                raise ValueError('Pilot identity was used in frozen native fit')
    saved = input_arrays(directory / 'inputs/baseline/external-predictions.npz')
    source_meta = load(directory / f'inputs/{SOURCE}.json')
    source_rows = [r for r, keep in zip(source_meta['rows'], saved[SOURCE + '__retained'], strict=True) if keep]
    cohort = {}
    for target in (SOURCE, *TRANSFERS):
        meta = load(directory / f'inputs/{target}.json')
        keep = saved[target + '__retained'].copy()
        # Include supplied recording IDs, artists/audio and declared component
        # groups when protecting source calibration identities from transfer.
        audit = None
        if target != SOURCE:
            disjoint, audit = exclusion(meta['rows'], source_rows)
            source_groups = {r['group'] for r in source_rows}
            source_recordings = {r.get('recording_id', r['id']) for r in source_rows}
            disjoint &= np.asarray([r['group'] not in source_groups and r.get('recording_id', r['id']) not in source_recordings for r in meta['rows']])
            keep &= disjoint
        data = input_arrays(directory / f'inputs/{target}.npz')
        calibration.check_groups(np.asarray(meta['groups'])[keep], data['outer'][keep])
        cohort[target] = {'rows': len(keep), 'retained_rows': int(keep.sum()),
                          'groups': len(set(np.asarray(meta['groups'])[keep])),
                          'indices': np.flatnonzero(keep).tolist(), 'source_overlap': audit}
    save(directory / 'inputs/cohorts.json', {'cohorts': cohort, 'native_exposure': audits})
    paths = code_paths()
    hashes = {relative: copy(path, directory / 'source' / relative) for relative, path in paths.items()}
    inputs = {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').rglob('*') if p.is_file()}
    save(directory / 'plan.json', {'format': 'vibe-score-contract-1', 'created_at': stamp(),
         'protocol_sha256': digest(PROTOCOL), 'code': hashes, 'inputs': inputs, 'references': references,
         'runtime': versions(), 'source': SOURCE, 'transfers': list(TRANSFERS), 'cohorts': cohort,
         'calibrators': list(calibration.FAMILIES), 'seed': calibration.SEED, 'bootstrap_draws': 1000,
         'thresholds': list(calibration.THRESHOLDS), 'product_adoption': False,
         'local_labels_opened': False, 'target_labels_used_for_fitting': False})
    print(json.dumps({'state': 'frozen', 'directory': str(directory),
                      'cohorts': {k: {'rows': v['retained_rows'], 'groups': v['groups']} for k, v in cohort.items()}}), flush=True)


def validate(directory, execution=False):
    plan = load(directory / 'plan.json')
    for relative, expected in plan['inputs'].items():
        path = inside(directory / relative)
        if not path.is_relative_to(directory) or digest(path) != expected:
            raise ValueError('Frozen input changed: ' + relative)
    current = code_paths()
    for relative, expected in plan['code'].items():
        if digest(directory / 'source' / relative) != expected or (execution and digest(current[relative]) != expected):
            raise ValueError('Frozen code changed: ' + relative)
    if execution and versions() != plan['runtime']:
        raise ValueError('Numerical runtime changed')
    return plan


def case_inputs(directory, target, plan):
    meta = load(directory / f'inputs/{target}.json')
    data = input_arrays(directory / f'inputs/{target}.npz')
    index = np.asarray(plan['cohorts'][target]['indices'])
    return meta, data, index, np.asarray(meta['groups'])[index], data['outer'][index]


def receipt(directory, name, outputs):
    save(directory / name, {'plan_sha256': digest(directory / 'plan.json'),
         'outputs': {p: digest(directory / p) for p in outputs}})


def check_receipt(directory, name):
    value = load(directory / name)
    if value['plan_sha256'] != digest(directory / 'plan.json'):
        raise ValueError('Receipt plan differs')
    for relative, expected in value['outputs'].items():
        path = inside(directory / relative)
        if not path.is_relative_to(directory) or digest(path) != expected:
            raise ValueError('Recorded output changed: ' + relative)
    return value


def fit(directory):
    plan = validate(directory, execution=True)
    if any((directory / name).exists() for name in ('fit.json', 'source-predictions.npz', 'fit.receipt.json')):
        raise ValueError('Preserve previous fits; use a new round')
    _, _, index, groups, outer = case_inputs(directory, SOURCE, plan)
    saved = input_arrays(directory / 'inputs/baseline/external-predictions.npz')
    x, y = (saved[SOURCE + '__cruise__' + key][index] for key in ('cruise_specialist', 'observed'))
    predictions, thresholds, diagnostics, models = calibration.nested(x, y, groups, outer)
    family, losses, inner_scores, inner_split = calibration.select(x, y, groups)
    threshold, threshold_details = calibration.choose_threshold(y, inner_scores, groups)
    save(directory / 'fit.json', {'created_at': stamp(), 'source': SOURCE, 'source_rows': len(x),
         'outer': diagnostics, 'outer_models': models, 'final_model': calibration.fit(family, x, y),
         'final_inner_mse': losses, 'final_inner_split': inner_split.tolist(),
         'final_threshold': threshold, 'final_threshold_training': threshold_details,
         'target_labels_used': False, 'local_labels_used': False})
    npz(directory / 'source-predictions.npz', **predictions, thresholds=thresholds)
    validate(directory, execution=True)
    receipt(directory, 'fit.receipt.json', ('fit.json', 'source-predictions.npz'))
    print(json.dumps({'state': 'fitted', 'family': family, 'threshold': threshold,
                      'outer_families': {f: row['family'] for f, row in diagnostics.items()},
                      'target_labels_used': False}), flush=True)


def metrics(y, score, split):
    result = summary(y, score, split)
    bins = []
    for i in range(10):
        mask = np.minimum((score * 10).astype(int), 9) == i
        if mask.any():
            bins.append({'left': i / 10, 'right': (i + 1) / 10, 'rows': int(mask.sum()),
                         'mean_score': float(score[mask].mean()), 'mean_mapped_rating': float(y[mask].mean())})
    result.update({'recording_mse': float(np.mean((y - score) ** 2)),
                   'recording_mae': float(np.mean(np.abs(y - score))), 'rating_bins': bins,
                   'rating_calibration_gap': float(sum(b['rows'] * abs(b['mean_score'] - b['mean_mapped_rating']) for b in bins) / len(y)),
                   'low_proxy_rows': int((y <= .3).sum()), 'high_proxy_rows': int((y >= .75).sum()),
                   'top_low_proxy_fraction': float(np.mean([discounted_gain((y[split == f] <= .3).astype(float), score[split == f], top_count(int((split == f).sum())))[1] for f in sorted(set(split))]))})
    return result


def ndcg(y, scores, split):
    values = []
    for fold in sorted(set(split)):
        target, score = y[split == fold], scores[split == fold]
        k = top_count(len(target))
        ideal = discounted_gain(target, target, k)[0]
        if np.ptp(target) > 1e-12 and ideal > 0:
            values.append(discounted_gain(target, score, k)[0] / ideal)
    return float(np.mean(values)) if values else None


def bootstrap(y, scores, split, groups, draws=1000):
    calibration.check_groups(groups, split)
    rng = np.random.default_rng(calibration.SEED)
    members = [[np.flatnonzero((groups == group) & (split == fold)) for group in sorted(set(groups[split == fold]))] for fold in sorted(set(split))]
    values = {name: [] for name in ('calibrated_vs_checkpoint_ndcg', 'calibrated_vs_raw_ndcg', 'raw_vs_checkpoint_ndcg', 'calibrated_vs_raw_mse_gain')}
    for _ in range(draws):
        index = np.concatenate([np.concatenate([part[i] for i in rng.integers(len(part), size=len(part))]) for part in members])
        n = {name: ndcg(y[index], scores[name][index], split[index]) for name in ('calibrated', 'raw', 'checkpoint')}
        for before, after, key in (('checkpoint', 'calibrated', 'calibrated_vs_checkpoint_ndcg'),
                                   ('raw', 'calibrated', 'calibrated_vs_raw_ndcg'),
                                   ('checkpoint', 'raw', 'raw_vs_checkpoint_ndcg')):
            if n[before] is not None and n[after] is not None:
                values[key].append(n[after] - n[before])
        values['calibrated_vs_raw_mse_gain'].append(float(np.mean((y[index] - scores['raw'][index])**2 - (y[index] - scores['calibrated'][index])**2)))
    return {name: {'conditional_95_interval': np.quantile(v, [.025, .975]).tolist() if v else None,
                   'usable_draws': len(v)} for name, v in values.items()}


def capture_scores(model, shared_models, data, count):
    """Batch replay, independently checked against scalar package/full baselines."""
    native = {source: replay(expert['model'], data['embedding']) for source, expert in model['experts'].items()}
    raw, embeddings = [], []
    for left, right in zip(data['offsets'][:-1], data['offsets'][1:], strict=True):
        stop = right if count is None else min(right, left + count)
        if stop <= left:
            raise ValueError('Empty excerpt input is not a numeric score')
        embeddings.append(data['embedding'][left:stop].mean(axis=0))
        parts = []
        for source, values in native.items():
            means = dict(zip(model['experts'][source]['target_names'], values[left:stop].mean(axis=0), strict=True))
            parts.append((1 - 2 * abs(means['arousal'] - .5) + means['valence']) / 2)
        raw.append(np.mean(parts))
    shared = {}
    for name, artifact in shared_models.items():
        values = np.asarray(embeddings)
        for stage in ('native', 'calibration'):
            group = artifact[stage]
            values = replay({'kind': 'ridge', 'medians': group['mean'], **group}, values)
        values = np.maximum(values, 1e-8)
        shared[name] = values / values.sum(axis=1, keepdims=True)
    first_left, first_right = data['offsets'][:2]
    first_stop = first_right if count is None else min(first_right, first_left + count)
    scalar = specialist.score(model, data['embedding'][first_left:first_stop].tolist())['score']
    if not np.isclose(raw[0], scalar, atol=1e-12, rtol=0):
        raise ValueError('Batch/scalar partial Cruise replay differs')
    return np.asarray(raw), shared


def capture_coverage(meta, data, index, count):
    captured = []
    for i in index:
        row = meta['rows'][i]
        chunks = row['chunks']
        if len(chunks) != data['offsets'][i+1] - data['offsets'][i]:
            raise ValueError('Capture metadata and embedding counts differ')
        chosen = chunks if count is None else chunks[:count]
        interval = row['covered_interval']
        intervals = [(c['start'], min(interval[1], c['start'] + c['seconds'])) for c in chosen]
        captured.append(coverage(intervals, interval))
    return captured


def evaluate(directory):
    plan = validate(directory, execution=True)
    check_receipt(directory, 'fit.receipt.json')
    if (directory / 'evaluation.report.json').exists() or (directory / 'evaluation.receipt.json').exists():
        raise ValueError('Preserve recorded evaluation; use a new round')
    fitted = load(directory / 'fit.json')
    source = input_arrays(directory / 'source-predictions.npz')
    saved = input_arrays(directory / 'inputs/baseline/external-predictions.npz')
    model = specialist.load(directory / 'inputs/baseline/cruise-specialist.json')
    shared_models = {name: load(directory / f'inputs/baseline/{name}.json') for name in CONTROLS}
    cases, outputs = {}, {}
    for target in (SOURCE, *TRANSFERS):
        meta, data, index, groups, outer = case_inputs(directory, target, plan)
        y = saved[target + '__cruise__observed'][index]
        raw = saved[target + '__cruise__cruise_specialist'][index]
        calibrated = source['selected'] if target == SOURCE else calibration.predict(fitted['final_model'], raw)
        scores = {name: saved[target + '__cruise__' + name][index] for name in CONTROLS}
        scores.update({'raw': raw, 'calibrated': calibrated})
        thresholds = source['thresholds'] if target == SOURCE else fitted['final_threshold']
        case = {'rows': len(index), 'groups': len(set(groups)), 'metrics': {name: metrics(y, value, outer) for name, value in scores.items()},
                'threshold': calibration.threshold_stats(y, calibrated, groups, thresholds),
                'paired': bootstrap(y, scores, outer, groups, plan['bootstrap_draws']), 'captures': {}}
        if target == SOURCE:
            case['family_diagnostics'] = {name: metrics(y, source[name], outer) for name in calibration.FAMILIES}
            case['outer_thresholds_available'] = all(row['threshold'] is not None for row in fitted['outer'].values())
        for label, count in (('one', 1), ('two', 2), ('all', None)):
            capture, shared = capture_scores(model, shared_models, data, count)
            capture = capture[index]
            converted = np.empty(len(index))
            if target == SOURCE:
                for fold in range(5):
                    mask = outer == fold
                    converted[mask] = calibration.predict(fitted['outer_models'][str(fold)], capture[mask])
            else:
                converted = calibration.predict(fitted['final_model'], capture)
            if count is None:
                if not np.allclose(capture, raw, atol=1e-12, rtol=0):
                    raise ValueError('Full Cruise differs from frozen baseline')
                for name in CONTROLS:
                    if not np.allclose(shared[name][index, 2], scores[name], atol=1e-12, rtol=0):
                        raise ValueError('Shared control differs from frozen baseline')
            captured = {'raw': capture, 'calibrated': converted,
                        **{name: values[index, 2] for name, values in shared.items()}}
            case['captures'][label] = {'metrics': {name: metrics(y, value, outer) for name, value in captured.items()},
                 'coverage': capture_coverage(meta, data, index, count),
                 'target': 'Full annotation interval; unheard portions remain unobserved',
                 'strong_match_allowed': False}
            for name, value in captured.items():
                outputs[f'{target}__{label}__{name}'] = value
        cases[target] = case
        print(json.dumps({'state': 'evaluated_domain', 'domain': target, 'rows': len(index)}), flush=True)
    def ranking_gate(variant):
        return (all(case['metrics'][variant]['ndcg'] >= case['metrics'][control]['ndcg'] + .02
                    and case['metrics'][variant]['top_low_proxy_fraction'] <= case['metrics']['checkpoint']['top_low_proxy_fraction'] + .05
                    for case in cases.values() for control in CONTROLS)
                and any(cases[target]['paired'][variant + '_vs_checkpoint_ndcg']['conditional_95_interval'][0] > 0 for target in TRANSFERS))
    scale = (cases[SOURCE]['metrics']['raw']['recording_mse'] - cases[SOURCE]['metrics']['calibrated']['recording_mse'] >= .002
             and all(cases[t]['metrics']['calibrated']['recording_mse'] <= cases[t]['metrics']['raw']['recording_mse'] + .005 for t in TRANSFERS)
             and all(c['metrics']['calibrated']['ndcg'] >= c['metrics']['raw']['ndcg'] - .02 for c in cases.values())
             and all(c['captures'][part]['metrics']['calibrated']['recording_mae'] <= c['metrics']['calibrated']['recording_mae'] + .05 for c in cases.values() for part in ('one', 'two')))
    report = {'created_at': stamp(), 'cases': cases, 'plan_sha256': digest(directory / 'plan.json'),
              'decisions': {'raw_ranking_candidate': bool(ranking_gate('raw')),
                            'calibrated_ranking_candidate': bool(ranking_gate('calibrated')), 'mapped_scale_candidate': bool(scale),
                            'strong_match_candidate': False,
                            'proxy_threshold_gates': bool(cases[SOURCE]['outer_thresholds_available'] and all(c['threshold']['passes'] for c in cases.values())),
                            'strong_match_blockers': ['No direct listening-purpose labels in these external domains.', 'Serving-domain and capture coverage validation remain outstanding.'],
                            'product_adoption': False},
              'supplement': 'Previously reported #570 reserved eight only; not read or fitted in this round.',
              'unsupported': ['Focus', 'Release', 'Unobserved recording intervals', 'Product strong-match probabilities', 'Unvalidated serving domains'],
              'limits': ['Previously examined external cohorts and previously selected fixed representation.', 'Mapped ratings are not listener-purpose suitability labels.', 'Unknown aliases/artists and upstream embedding training overlap remain unresolved.', 'Conditional group-bootstrap intervals do not include representation/earlier selection uncertainty.', 'Memo annotation trajectory and ManyMusic listener ratings differ; no pooled-domain acceptance.']}
    save(directory / 'evaluation.report.json', report)
    npz(directory / 'evaluation-predictions.npz', **outputs)
    validate(directory, execution=True)
    check_receipt(directory, 'fit.receipt.json')
    receipt(directory, 'evaluation.receipt.json', ('evaluation.report.json', 'evaluation-predictions.npz', 'fit.receipt.json'))
    print(json.dumps({'state': 'evaluated', 'decisions': report['decisions']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'fit', 'evaluate', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    # Share the normal machine-wide test/build guard, including its bounded wait.
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
        elif args.action == 'fit':
            fit(directory)
        elif args.action == 'evaluate':
            evaluate(directory)
        else:
            validate(directory)
            check_receipt(directory, 'fit.receipt.json')
            check_receipt(directory, 'evaluation.receipt.json')
            print(json.dumps({'state': 'verified', 'directory': str(directory)}), flush=True)


if __name__ == '__main__':
    main()
