"""Evaluate frozen Release mappings on unchanged native human references."""
import json
from pathlib import Path
import resource
import time

import numpy as np

from ..studies import individual_protocol as protocol
from . import release_models as models
from ..support.external_experiment import digest, save
from ..focus.focus_experiment import arrays, check_receipt, load
from ..studies.individual_evaluation import annotation_slices, shared_prediction
from ..studies.individual_experiment import compact, receipt
from .release_experiment import (ATTRIBUTES, EMO, JAM, SOUND, SOURCES, TARGET,
                                 dataset, validate)


def prior_head(directory, source):
    original = load(directory / f'inputs/{source}.embedding.json')
    return original['heads'][original['target_names'].index(TARGET[source])]


def scores_for(model, x):
    return {'new_anchor': models.native_predict(model['anchor'], x),
            'monotone': models.mapping_predict(model['mapping'], x),
            'zero_weight': models.mapping_predict(model['mapping'], x, zero=True),
            'native_tasks': models.tasks_predict(model['native_tasks'], x, model['anchor_source'])}


def comparison(y, scores, outer, groups, comparisons, draws):
    if not all(np.isfinite(value).all() and len(value) == len(y) for value in scores.values()):
        raise ValueError('Incomplete common-row predictions')
    return {'rows': len(y), 'groups': len(set(groups)),
            'metrics': {name: protocol.metrics(y, value, outer) for name, value in scores.items()},
            'native_error': {name: {'mse': float(np.mean((value - y) ** 2)),
                                   'mae': float(np.mean(np.abs(value - y)))} for name, value in scores.items()},
            'paired': protocol.paired(y, scores, outer, groups, comparisons, draws)}


def native_reports(directory, plan):
    reports, predictions = {}, {}
    for source in SOURCES:
        data = dataset(directory, source)
        scores = arrays(directory / (source + '.native.npz'))
        original = arrays(directory / f'inputs/{source}.native-oof.npz')
        full = arrays(directory / f'inputs/{source}.npz')
        meta = load(directory / f'inputs/{source}.json')
        saved = original['embedding']
        old_indices = np.flatnonzero(full['native_keep'])
        wanted = full['release_keep'][old_indices]
        scores['legacy_native_oof'] = saved[wanted, meta['target_names'].index(TARGET[source])]
        pairs = [(name, 'anchor') for name in ('monotone', 'zero_weight', 'native_tasks',
                                              'observed_attributes', 'predicted_attributes')]
        pairs += [('observed_attributes', 'predicted_attributes')]
        result = comparison(data['y'], scores, data['outer'], data['groups'], pairs, plan['bootstrap_draws'])
        result.update(target=TARGET[source], diagnostic_inputs=ATTRIBUTES[source],
                      legacy_limit='Legacy OOF retains its larger original training population; compare new methods with the restricted anchor.',
                      diagnostic_limit='Observed attributes are unavailable at inference. Same frozen mapping on observed/predicted attributes diagnoses information loss, not a new product target.')
        reports[source] = result
        predictions.update({source + '__' + name: value for name, value in scores.items()})
        predictions[source + '__observed'], predictions[source + '__outer'] = data['y'], data['outer']
        print(json.dumps({'native_evaluated': source, 'rows': len(data['y']),
                          'ndcg': {k: round(v['ndcg'], 4) for k, v in result['metrics'].items()}}), flush=True)
    return reports, predictions


def transfer_reports(directory, plan):
    reports, predictions = {}, {}
    for target in SOURCES:
        data = arrays(directory / f'inputs/{target}.npz')
        meta = load(directory / f'inputs/{target}.json')
        keep = data['evaluation_keep']
        y = data['y'][keep, meta['target_names'].index(TARGET[target])]
        model = load(directory / f'models/transfer-{target}.json')
        scores = {name: value[keep] for name, value in scores_for(model, data['embedding']).items()}
        scores['legacy_anchor'] = models.native_predict(prior_head(directory, model['anchor_source']), data['embedding'])[keep]
        training = dataset(directory, model['anchor_source'])
        scores['training_mean'] = np.full(len(y), training['y'].mean())
        scores['training_median'] = np.full(len(y), np.median(training['y']))
        groups, outer = np.asarray(meta['groups'])[keep], data['outer'][keep]
        pairs = [(name, control) for name in ('monotone', 'native_tasks', 'zero_weight')
                 for control in ('legacy_anchor', 'new_anchor', 'training_mean')]
        reports[target] = comparison(y, scores, outer, groups, pairs, plan['bootstrap_draws'])
        reports[target].update(target=TARGET[target], anchor=TARGET[model['anchor_source']],
                               auxiliary=TARGET[model['auxiliary_source']],
                               training_sources=list(model['training_ids']),
                               limit='Whole-corpus transfer on previously inspected populations; construct scales remain distinct.')
        predictions.update({target + '__' + name: value for name, value in scores.items()})
        predictions[target + '__observed'], predictions[target + '__outer'] = y, outer
        print(json.dumps({'transfer_evaluated': target, 'rows': len(y),
                          'ndcg': {k: round(v['ndcg'], 4) for k, v in reports[target]['metrics'].items()}}), flush=True)
    return reports, predictions


def primary_report(directory, plan):
    data = arrays(directory / f'inputs/{SOUND}.npz')
    meta = load(directory / f'inputs/{SOUND}.json')
    prior = arrays(directory / 'inputs/primary.predictions.npz')
    old_report = load(directory / 'inputs/primary.report.json')[SOUND]['vibes']['release']
    prefix = SOUND + '__release__'
    expected_y = data['y'][:, meta['target_names'].index('anger')]
    if not np.array_equal(prior[prefix + 'observed'], expected_y):
        raise ValueError('Legacy primary reference/order is not the complete Soundtracks cohort')
    keep = data['common']
    scores = {'old_' + name: prior[prefix + name][keep] for name in old_report['metrics']}
    model = load(directory / f'models/transfer-{SOUND}.json')
    for preparation, field in (('historical', 'embedding'), ('browser', 'browser')):
        for name, value in scores_for(model, data[field]).items():
            scores[name + '_' + preparation] = value[keep]
        replay = models.native_predict(prior_head(directory, JAM), data[field])[keep]
        original = scores['old_' + JAM + ('_embedding' if preparation == 'historical' else '_browser')]
        if not np.allclose(replay, original, atol=1e-12, rtol=0):
            raise ValueError('Exact Jamendo baseline replay differs')
    y, outer = expected_y[keep], data['outer'][keep]
    pairs = []
    for preparation in ('historical', 'browser'):
        controls = ('old_' + JAM + ('_embedding' if preparation == 'historical' else '_browser'),
                    'old_checkpoint', 'old_refresh_calibration', 'old_refresh_both',
                    'old_browser_shared', 'new_anchor_' + preparation)
        pairs.extend((method + '_' + preparation, control) for method in ('monotone', 'native_tasks', 'zero_weight')
                     for control in controls)
    result = comparison(y, scores, outer, np.asarray(meta['groups'])[keep], pairs, plan['bootstrap_draws'])
    result['slices'] = annotation_slices(SOUND, 'release', data, meta, keep, scores, y, plan['attribute_names'])
    names = meta['target_names']
    get = lambda name: data['y'][:, names.index(name)]
    selected = ((get('energy') >= .65) & (get('happy') >= .65) & (get('anger') <= .35))[keep]
    result['slices']['energetic_happy_low_anger'] = {
        'rows': int(selected.sum()), 'reference_mean': float(y[selected].mean()) if selected.any() else None,
        'score_means': {name: float(value[selected].mean()) for name, value in scores.items()} if selected.any() else {}}
    result['exposure'] = plan['exposure']
    return result, {'observed': y, 'outer': outer, **scores}


def independent_mapping(model, x, zero=False):
    a = models.native_predict(model['anchor'], x, independent=True)
    b = models.native_predict(model['auxiliary'], x, independent=True)
    calibration = model['zero_weight_calibration'] if zero else model['calibration']
    weight = 0 if zero else model['weight']
    return np.asarray([min(1., max(0., calibration['intercept'] + calibration['slope'] * ((1 - weight) * aa + weight * bb)))
                       for aa, bb in zip(a, b, strict=True)])


def resources(directory, plan):
    reports, outputs = {}, []
    for target in SOURCES:
        model = load(directory / f'models/transfer-{target}.json')
        x = arrays(directory / f'inputs/{target}.npz')['embedding']
        for name in ('anchor', 'mapping', 'native_tasks'):
            artifact = {'format': 'release-614-scorer-v1', 'model': model[name],
                        'anchor_source': model['anchor_source'], 'target_excluded': target,
                        'embedding_signature': plan['embedding_signature'],
                        'plan_sha256': digest(directory / 'plan.json'), 'research_only': True,
                        'terms': plan['jamendo_new_artifacts']}
            path = directory / f'scorers/{target}.{name}.json'
            compact(path, artifact)
            start = time.perf_counter()
            restored = load(path)['model']
            load_ms = (time.perf_counter() - start) * 1000
            if name == 'anchor':
                fn = lambda xx: models.native_predict(restored, xx)
                independent = models.native_predict(restored, x, True)
            elif name == 'mapping':
                fn = lambda xx: models.mapping_predict(restored, xx)
                independent = independent_mapping(restored, x)
                zero_difference = float(np.max(np.abs(models.mapping_predict(restored, x, zero=True) - independent_mapping(restored, x, True))))
                if zero_difference > 1e-10:
                    raise ValueError('Independent zero-weight replay differs')
            else:
                fn = lambda xx: models.tasks_predict(restored, xx, model['anchor_source'])
                independent = models.independent_tasks_predict(restored, x, model['anchor_source'])
                for native_target in restored['targets']:
                    difference = np.max(np.abs(models.tasks_predict(restored, x, native_target) -
                                               models.independent_tasks_predict(restored, x, native_target)))
                    if difference > 1e-10:
                        raise ValueError('Independent native task head differs')
            replay_error = float(np.max(np.abs(fn(x) - independent)))
            if replay_error > 1e-10:
                raise ValueError('Independent scorer replay differs')
            fn(x[:3])
            elapsed = []
            for _ in range(100):
                start = time.perf_counter()
                fn(x[:3])
                elapsed.append((time.perf_counter() - start) * 1000)
            bound = plan['budgets']['mapping_bytes' if name == 'mapping' else 'single_bytes']
            reports[target + '__' + name] = {
                'bytes': path.stat().st_size, 'load_ms': load_ms,
                'warm_three_max_ms': max(elapsed), 'warm_three_median_ms': float(np.median(elapsed)),
                'independent_replay_error': replay_error,
                'within_size_and_inference_bounds': path.stat().st_size <= bound and max(elapsed) <= plan['budgets']['warm_three_ms']}
            outputs.append(path)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return {'scorers': reports, 'peak_process_rss_bytes': rss, 'within_rss_bound': rss <= plan['budgets']['rss_bytes'],
            'scope': 'Python scorer inference/load; encoder and application startup excluded.'}, outputs


def local_report(directory, plan):
    local = Path(plan['local'])
    rows = load(local / 'labels.json')['rows']
    model = load(directory / f'models/transfer-{SOUND}.json')
    previous = prior_head(directory, JAM)
    checkpoint = load(directory / 'inputs/checkpoint.json')
    records, incompatible = [], []
    for row in rows:
        samples = [load(local / path) for path in row['sample_files']]
        x = np.asarray([sample['embedding']['values'] for sample in samples])
        if row['embedding_signature'] != plan['embedding_signature'] or x.shape != (len(samples), 200):
            incompatible.append(row['id'])
            continue
        values = {}
        for label, xx in (('first', x[:1]), ('three', x[:3]), ('all', x)):
            a = models.native_predict(model['mapping']['anchor'], xx).mean()
            b = models.native_predict(model['mapping']['auxiliary'], xx).mean()
            mapping = model['mapping']
            values[label] = {
                'legacy_jamendo': float(models.native_predict(previous, xx).mean()),
                'new_anchor': float(models.native_predict(model['anchor'], xx).mean()),
                'monotone': float(models.affine_predict(mapping['calibration'], (1 - mapping['weight']) * a + mapping['weight'] * b)),
                'zero_weight': float(models.affine_predict(mapping['zero_weight_calibration'], a)),
                'native_tasks': float(models.tasks_predict(model['native_tasks'], xx, JAM).mean()),
                'checkpoint': float(shared_prediction(checkpoint, xx.mean(axis=0, keepdims=True))[0, checkpoint['labels'].index('release')])}
        records.append({'id': row['id'], 'samples': len(samples), 'release_positive': 'release' in row['labels'],
                        'prior_inspected': row['prior_inspected'], 'scores': values})
    return {'rows': len(rows), 'compatible_rows': len(records), 'incompatible_ids': incompatible,
            'explicit_release_positives': sum('release' in row['labels'] for row in rows),
            'new_identities_since_570': sum(not row['prior_inspected'] for row in rows),
            'partial_capture_rows': sum(len(row['sample_files']) < 3 for row in rows),
            'records': records,
            'limit': 'Supplementary positive labels and capture consistency only; unselected vibes unknown, no false-positive or causal benefit estimates.'}


def decisions(primary, transfer, plan, resource_report):
    result = {}
    resources_pass = resource_report['within_rss_bound'] and all(
        row['within_size_and_inference_bounds'] for row in resource_report['scorers'].values())
    for name in ('monotone', 'native_tasks'):
        evidence, primary_pass = {}, True
        for preparation, suffix in (('historical', '_embedding'), ('browser', '_browser')):
            candidate = name + '_' + preparation
            comparisons = {}
            for control in ('old_' + JAM + suffix, 'old_checkpoint'):
                paired = primary['paired'][candidate + '__vs__' + control]
                metrics = paired['ndcg'], paired['top_lift']
                thresholds = (.02, .01) if control != 'old_checkpoint' else (0., 0.)
                passed = all(value['difference'] >= threshold and value['conditional_95_percent_interval'][0] > 0
                             for value, threshold in zip(metrics, thresholds, strict=True))
                primary_pass &= passed
                comparisons[control] = {'passes': passed, 'paired': paired}
            evidence[preparation] = comparisons
        regressions = {}
        for target in (JAM, EMO):
            paired = transfer[target]['paired'][name + '__vs__legacy_anchor']
            regressions[target] = {'material_point_loss': paired['ndcg']['difference'] < -.02 or paired['top_lift']['difference'] < -.01,
                                   'paired': paired}
        reasons = []
        if not primary_pass:
            reasons.append('Primary ranking/top-list improvements do not meet all frozen gain and uncertainty requirements.')
        if any(value['material_point_loss'] for value in regressions.values()):
            reasons.append('At least one reciprocal native transfer has a material point regression requiring explanation.')
        if not resources_pass:
            reasons.append('At least one resource bound failed.')
        if not plan['acceptance']['independent_confirmation_available']:
            reasons.append('This existing-data round supplies no independent confirmation population.')
        result[name] = {'recommendation': 'retain', 'primary_pass': primary_pass, 'primary_evidence': evidence,
                        'reciprocal_regressions': regressions,
                        'confirmation_available': plan['acceptance']['independent_confirmation_available'],
                        'reasons': reasons}
    return {'overall_recommendation': 'retain incumbent; preserve exact Jamendo research candidate',
            'candidates': result, 'resources_pass': resources_pass,
            'acceptance_status': 'Author recommendation; independent review required.'}


def run(directory):
    plan = validate(directory, execution=True)
    if (directory / 'evaluation.receipt.json').exists():
        check_receipt(directory, 'evaluation.receipt.json')
        print(json.dumps({'already_evaluated': str(directory)}), flush=True)
        return
    for name in ('fit-native', *('fit-transfer-' + s for s in SOURCES)):
        check_receipt(directory, name + '.receipt.json')
    reports, predictions = native_reports(directory, plan)
    transfer, transfer_predictions = transfer_reports(directory, plan)
    primary, primary_predictions = primary_report(directory, plan)
    resource_report, outputs = resources(directory, plan)
    for name, value in (('native.report.json', reports), ('transfer.report.json', transfer),
                        ('primary.report.json', primary), ('resources.report.json', resource_report),
                        ('local.report.json', local_report(directory, plan)),
                        ('decisions.json', decisions(primary, transfer, plan, resource_report))):
        path = directory / name
        save(path, value)
        outputs.append(path)
    for name, value in (('native.predictions.npz', predictions), ('transfer.predictions.npz', transfer_predictions),
                        ('primary.predictions.npz', primary_predictions)):
        path = directory / name
        np.savez_compressed(path, **value)
        outputs.append(path)
    validate(directory, execution=True)
    receipt(directory, 'evaluation', outputs)
    print(json.dumps({'evaluated': str(directory), 'primary_rows': primary['rows'],
                      'primary_ndcg': {k: round(v['ndcg'], 4) for k, v in primary['metrics'].items()
                                       if k.startswith(('monotone', 'native_tasks', 'new_anchor', 'zero_weight'))},
                      'recommendation': 'retain'}), flush=True)
