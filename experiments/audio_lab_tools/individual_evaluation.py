"""Evaluate already-frozen individual heads; target labels never select models."""
import json
from pathlib import Path
import resource
import time

import numpy as np

from . import individual_protocol as protocol
from .external_experiment import digest, save, stamp
from .focus_experiment import arrays, check_receipt, head_predictions, load
from .mapped_vibe_inference import replay
from .mapped_vibe_rules import apply, labels


def prediction(model, data, preparation='historical', limit=None):
    family = model['family']
    if preparation == 'browser':
        if family != 'embedding':
            raise ValueError('No aligned browser acoustic attributes')
        x = data['browser']
    else:
        x = protocol.features(data, family)
    native = head_predictions(model['heads'], x, True)
    if 'offsets' in data:
        native = protocol.mean_chunks(native, data['offsets'], limit)
    return native


def shared_prediction(model, x):
    for key in ('native', 'calibration'):
        stage = model[key]
        x = replay({'kind': 'ridge', 'medians': stage['mean'], **stage}, x)
    x = np.maximum(x, 1e-8)
    return x / x.sum(axis=1, keepdims=True)


def native_report(directory, source, data, meta, rules):
    keep = data['native_keep']
    y, outer = data['y'][keep], data['outer'][keep]
    candidates = arrays(directory / (source + '.native-oof.npz'))
    old = arrays(directory / f'inputs/{source}.historic.npz')
    candidates['prior_native_mae'] = old['prior_native'][keep]
    metrics = {method: {name: {'mse': float(np.mean((y[:, c] - value[:, c]) ** 2)),
                               'mae': float(np.mean(np.abs(y[:, c] - value[:, c])))}
                        for c, name in enumerate(meta['target_names'])} for method, value in candidates.items()}
    mapped = {}
    for vibe, rule in rules.items():
        if vibe not in ('release', 'energize', 'relax', 'uplift'):
            continue
        observed = apply(rule['expression'], y, meta['target_names'])
        scores = {method: apply(rule['expression'], value, meta['target_names']) for method, value in candidates.items()}
        scores.update({method: old[vibe + '__' + suffix][keep]
                       for method, suffix in (('prior_mapped_selector', 'selected'), ('prior_native_mse', 'native_mse'))})
        mapped[vibe] = {'semantics': rule['semantics'],
                        'metrics': {method: protocol.metrics(observed, value, outer) for method, value in scores.items()}}
    return {'rows': len(y), 'groups': len(set(np.asarray(meta['groups'])[keep])),
            'native': metrics, 'mapped': mapped, 'exposure': 'Previously inspected native cohorts; held-out source folds only.'}


def candidate_scores(target, vibe, data, rules, models, directory, metadata):
    from .individual_experiment import PILOTS
    expression = rules[target][vibe]['expression']
    observed = apply(expression, data['y'], metadata[target]['target_names'])
    scores, means, new = {}, {}, []
    sources = (('pmemo', 'deam', 'emotify-artist') if target in PILOTS or target == 'cal500-raw-activity'
               else ('emotify-artist', 'jamendo-human-4shards', 'coimbra-mirex-like', 'emma-preview-matched', 'pmemo', 'deam'))
    for source in sources:
        if vibe not in rules[source]:
            continue
        rule = rules[source][vibe]['expression']
        for family in protocol.FAMILIES:
            if target in PILOTS and family != 'embedding':
                continue
            model = models[source, family]
            name = source + '_' + family
            scores[name] = apply(rule, prediction(model, data), model['target_names'])
            new.append(name)
            means[name] = source + '_mean'
        model = models[source, 'embedding']
        for kind in ('mean', 'median'):
            value = apply(rule, np.asarray(model['source_' + kind]), model['target_names'])
            scores[source + '_' + kind] = np.full(len(observed), value)
        name = source + '_browser'
        scores[name] = apply(rule, prediction(model, data, 'browser'), model['target_names'])
        new.append(name)
        means[name] = source + '_mean'
        regime = 'pilots' if target in PILOTS else 'soundtracks'
        path = directory / f'inputs/prior-{regime}-{source}.predictions.npz'
        if target != 'cal500-raw-activity' and path.exists():
            old = arrays(path)
            key = target + ('__mean_native_then_map__' if target in PILOTS else '__aligned_excerpt__') + vibe + '__selected'
            if key in old:
                scores[source + '_prior_selected'] = old[key]
    if vibe in ('energize', 'relax', 'uplift'):
        for preparation in ('embedding', 'browser'):
            name = 'equal_va_' + preparation
            scores[name] = (scores['pmemo_' + preparation] + scores['deam_' + preparation]) / 2
            means[name] = 'equal_va_mean'
            new.append(name)
        scores['equal_va_mean'] = (scores['pmemo_mean'] + scores['deam_mean']) / 2
    if vibe == 'release' and target == 'soundtracks-all':
        for preparation in ('embedding', 'browser'):
            name = 'equal_release_' + preparation
            scores[name] = (scores['emotify-artist_' + preparation] + scores['jamendo-human-4shards_' + preparation]) / 2
            means[name] = 'equal_release_mean'
            new.append(name)
        scores['equal_release_mean'] = (scores['emotify-artist_mean'] + scores['jamendo-human-4shards_mean']) / 2
    priors = load(directory / 'inputs/named-priors.json')
    prior = priors[vibe]
    family = {'attributes_embedding': 'combined'}.get(prior['family'], prior['family'])
    if target != 'cal500-raw-activity' and (target not in PILOTS or family == 'embedding'):
        native = replay(prior['model'], protocol.features(data, family))
        if 'offsets' in data:
            native = protocol.mean_chunks(native, data['offsets'])
        scores['named_prior'] = apply(prior['rule']['expression'], native, prior['target_names'])
    if target == 'cal500-raw-activity':
        old = arrays(directory / f'inputs/{target}.historic.npz')
        scores['prior_native_mae'] = apply(expression, old['prior_native'], metadata[target]['target_names'])
        saved = arrays(directory / (target + '.native-oof.npz'))
        retained = data['native_keep']
        for method, native in saved.items():
            full = np.full(len(observed), np.nan)
            full[retained] = apply(expression, native, metadata[target]['target_names'])
            name = 'activity_native_' + method
            scores[name] = full
            if method in protocol.FAMILIES:
                new.append(name)
                means[name] = 'activity_native_training_mean'
        if not np.all(retained[data['common']]):
            raise ValueError('Native OOF cannot cover the common activity rows')
    return observed, scores, new, means


def annotation_slices(target, vibe, data, meta, keep, scores, observed, attribute_names):
    names, y = meta['target_names'], data['y']
    slices = {}
    if target == 'soundtracks-all':
        get = lambda name: y[:, names.index(name)]
        slices = {'high_anger_low_energy': (get('anger') >= .65) & (get('energy') <= .35),
                  'high_energy_low_anger': (get('energy') >= .65) & (get('anger') <= .35),
                  'gentle_happy': (get('happy') >= .65) & (get('energy') <= .35),
                  'energetic_unhappy': (get('energy') >= .65) & (get('valence') <= .35),
                  'low_energy_tense': (get('energy') <= .35) & (get('tension') >= .65),
                  'low_energy_sad': (get('energy') <= .35) & (get('sad') >= .65)}
        tempo = [name for name in attribute_names if 'bpm' in name.lower()]
        if len(tempo) == 1:
            slices['feature_tempo_at_most_90_high_anger'] = (data['attributes'][:, attribute_names.index(tempo[0])] <= 90) & (get('anger') >= .65)
    elif 'valence' in names and 'arousal' in names:
        valence, arousal = y[:, names.index('valence')], y[:, names.index('arousal')]
        slices = {'gentle_positive': (valence >= .65) & (arousal <= .35),
                  'energetic_negative': (valence <= .35) & (arousal >= .65),
                  'low_activation_negative': (valence <= .35) & (arousal <= .35)}
    result = {}
    for name, mask in slices.items():
        selected = mask[keep]
        result[name] = {'rows': int(selected.sum()),
                        'reference_mean': float(observed[selected].mean()) if selected.any() else None,
                        'score_means': {method: float(value[selected].mean()) for method, value in scores.items()} if selected.any() else {},
                        'limit': 'Descriptive fixed annotation/feature slice; empty means unknown; no slice-based selection.'}
    return result


def primary_reports(directory, plan, datasets, metadata, rules, models):
    from .individual_experiment import PRIMARY, VIBES
    shared = arrays(directory / 'inputs/shared.npz')
    browser = arrays(directory / 'inputs/browser_shared.npz')
    reports, predictions = {}, {}
    for target in PRIMARY:
        data, meta = datasets[target], metadata[target]
        keep = data['common']
        outer, groups = data['outer'][keep], np.asarray(meta['groups'])[keep]
        reports[target] = {'rows': int(keep.sum()), 'groups': len(set(groups)),
                           'exclusions': meta['common_exclusions'], 'vibes': {}}
        for vibe in VIBES:
            if vibe not in rules[target]:
                continue
            observed, scores, new, means = candidate_scores(target, vibe, data, rules, models, directory, metadata)
            prefix = target + '__' + vibe + '__'
            if not np.array_equal(observed, shared[prefix + 'observed']) or not np.array_equal(observed, browser[prefix + 'observed']):
                raise ValueError('Fixed target mappings differ from accepted controls')
            for name in ('checkpoint', 'refresh_calibration', 'refresh_both'):
                scores[name] = shared[prefix + name]
            scores['browser_shared'] = browser[prefix + 'browser_shared']
            scores['historical_ffmpeg_shared'] = browser[prefix + 'ffmpeg_shared']
            observed = observed[keep]
            scores = {name: value[keep] for name, value in scores.items()}
            if not all(np.isfinite(value).all() for value in scores.values()):
                raise ValueError('Incomplete common-reference predictions')
            comparisons = [(name, control) for name in new for control in
                           ('checkpoint', 'refresh_calibration', 'refresh_both', 'browser_shared', means[name])]
            if 'named_prior' in scores:
                comparisons += [(name, 'named_prior') for name in new]
            report = {'semantics': rules[target][vibe]['semantics'],
                      'metrics': {name: protocol.metrics(observed, value, outer) for name, value in scores.items()},
                      'paired': protocol.paired(observed, scores, outer, groups, comparisons, plan['bootstrap_draws']),
                      'slices': annotation_slices(target, vibe, data, meta, keep, scores, observed, plan['attribute_names'])}
            reports[target]['vibes'][vibe] = report
            predictions[prefix + 'observed'], predictions[prefix + 'outer'] = observed, outer
            predictions.update({prefix + name: value for name, value in scores.items()})
            print(json.dumps({'evaluated': target, 'vibe': vibe, 'rows': len(observed),
                              'checkpoint_ndcg': report['metrics']['checkpoint']['ndcg'],
                              'methods': len(scores)}), flush=True)
    return reports, predictions


def reciprocal_reports(plan, datasets, metadata, rules, models):
    result, predictions = {}, {}
    for source, target in plan['reciprocal']:
        data, meta = datasets[target], metadata[target]
        keep = data['native_keep']
        for vibe in ('release', 'energize', 'relax', 'uplift'):
            if vibe not in rules[source] or vibe not in rules[target]:
                continue
            # Power/anger/aggression reciprocity is the Release task only.
            if source not in ('pmemo', 'deam') and vibe != 'release':
                continue
            observed = apply(rules[target][vibe]['expression'], data['y'], meta['target_names'])[keep]
            scores = {}
            for family in protocol.FAMILIES:
                model = models[source, family]
                scores[family] = apply(rules[source][vibe]['expression'], prediction(model, data), model['target_names'])[keep]
            for kind in ('mean', 'median'):
                value = apply(rules[source][vibe]['expression'], np.asarray(model['source_' + kind]), model['target_names'])
                scores['source_' + kind] = np.full(len(observed), value)
            key = source + '__to__' + target + '__' + vibe
            result[key] = {'rows': len(observed), 'source_construct': rules[source][vibe]['expression'],
                           'target_construct': rules[target][vibe]['expression'],
                           'metrics': {name: protocol.metrics(observed, value, data['outer'][keep]) for name, value in scores.items()}}
            predictions[key + '__observed'] = observed
            predictions.update({key + '__' + name: value for name, value in scores.items()})
    return result, predictions


def resource_reports(directory, plan, models, rules, datasets):
    from .individual_experiment import compact
    results, paths = {}, []
    for (source, family), model in models.items():
        x = protocol.features(datasets[source], family)[:3]
        for vibe in ('release', 'energize', 'relax', 'uplift'):
            if vibe not in rules[source]:
                continue
            wanted = [name for name in model['target_names'] if name in labels(rules[source][vibe]['expression'])]
            slim = {**model, 'vibe': vibe, 'rule': rules[source][vibe], 'target_names': wanted,
                    'heads': [model['heads'][model['target_names'].index(name)] for name in wanted]}
            # Diagnostic means retain their original native order in the main model only.
            slim.pop('source_mean')
            slim.pop('source_median')
            path = directory / f'scorers/{source}.{family}.{vibe}.json'
            compact(path, slim)
            paths.append(path)
            start = time.perf_counter()
            saved = load(path)
            load_ms = (time.perf_counter() - start) * 1000
            expected = prediction(model, {'embedding': datasets[source]['embedding'][:3],
                'attributes': datasets[source]['attributes'][:3]})[:, [model['target_names'].index(name) for name in wanted]]
            replayed = head_predictions(saved['heads'], x, True)
            error = float(np.max(np.abs(expected - replayed)))
            if error > 1e-10:
                raise ValueError('Slim per-vibe export changed a native head')
            times = []
            for _ in range(100):
                start = time.perf_counter()
                apply(saved['rule']['expression'], head_predictions(saved['heads'], x, True), wanted)
                times.append((time.perf_counter() - start) * 1000)
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            results[source + '.' + family + '.' + vibe] = {'bytes': path.stat().st_size, 'load_ms': load_ms,
                'warm_three_excerpt_median_ms': float(np.median(times)), 'warm_three_excerpt_max_ms': max(times),
                'process_peak_rss_bytes': rss, 'per_head_replay_max_error': error,
                'budget_pass': path.stat().st_size <= plan['budgets']['single_scorer_bytes'] and
                    max(times) <= plan['budgets']['warm_three_excerpt_ms'] and rss <= plan['budgets']['process_rss_bytes']}
    for vibe, sources in (('release', ('emotify-artist', 'jamendo-human-4shards')),
                          *[(vibe, ('pmemo', 'deam')) for vibe in ('energize', 'relax', 'uplift')]):
        members = [load(directory / f'scorers/{source}.embedding.{vibe}.json') for source in sources]
        path = directory / f'scorers/equal-ensemble.{vibe}.json'
        compact(path, {'format': 'equal-native-source-ensemble-v1', 'vibe': vibe,
                       'plan_sha256': digest(directory / 'plan.json'), 'weights': [.5, .5], 'members': members})
        paths.append(path)
        x = datasets[sources[0]]['embedding'][:3]
        start = time.perf_counter()
        saved = load(path)
        load_ms = (time.perf_counter() - start) * 1000
        times = []
        for _ in range(100):
            start = time.perf_counter()
            values = np.mean([apply(member['rule']['expression'], head_predictions(member['heads'], x, True),
                                     member['target_names']) for member in saved['members']], axis=0)
            times.append((time.perf_counter() - start) * 1000)
        expected = np.mean([apply(member['rule']['expression'], head_predictions(member['heads'], x),
                                  member['target_names']) for member in members], axis=0)
        error = float(np.max(np.abs(values - expected)))
        if error > 1e-10:
            raise ValueError('Ensemble independent replay differs')
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        results['equal-ensemble.' + vibe] = {'bytes': path.stat().st_size, 'load_ms': load_ms,
            'warm_three_excerpt_median_ms': float(np.median(times)), 'warm_three_excerpt_max_ms': max(times),
            'process_peak_rss_bytes': rss, 'per_head_replay_max_error': error,
            'budget_pass': path.stat().st_size <= plan['budgets']['ensemble_bytes'] and
                max(times) <= plan['budgets']['warm_three_excerpt_ms'] and rss <= plan['budgets']['process_rss_bytes']}
    return results, paths


def excerpt_diagnostics(datasets, metadata, rules, models):
    from .individual_experiment import PILOTS
    result = {}
    for target in PILOTS:
        data, meta = datasets[target], metadata[target]
        keep = data['common']
        for source in ('pmemo', 'deam', 'emotify-artist'):
            model = models[source, 'embedding']
            for vibe in ('energize', 'relax', 'uplift'):
                expression = rules[source][vibe]['expression']
                predictions = {str(limit or 'all'): apply(expression, prediction(model, data, limit=limit), model['target_names'])
                               for limit in (1, 3, None)}
                record = {'rows': int(keep.sum()), 'first_to_all_mean_absolute_change': float(np.abs(predictions['1'][keep] - predictions['all'][keep]).mean()),
                          'three_to_all_mean_absolute_change': float(np.abs(predictions['3'][keep] - predictions['all'][keep]).mean()),
                          'first_to_all_max_absolute_change': float(np.abs(predictions['1'][keep] - predictions['all'][keep]).max()),
                          'chunk_truth': 'Unavailable: reference labels cover the full rated window.'}
                if target == 'memo2496-pilot':
                    truth = {str(limit or 'all'): apply(rules[target][vibe]['expression'],
                             np.asarray([np.asarray(row['chunk_targets'])[:limit].mean(axis=0) for row in meta['rows']]), meta['target_names'])
                             for limit in (1, 3, None)}
                    record['chunk_truth'] = 'Released expert trajectories averaged over exactly the first/three/all rated chunks.'
                    record['aligned_mse'] = {key: float(np.mean((truth[key][keep] - values[keep]) ** 2)) for key, values in predictions.items()}
                result[target + '__' + source + '__' + vibe] = record
    return result


def local_diagnostics(directory, plan, models, rules):
    local = Path(plan['local'])
    check_receipt(local, 'receipt.json', False)
    rows = load(local / 'labels.json')['rows']
    checkpoint = load(directory / 'inputs/checkpoint.json')
    records = []
    for row in rows:
        samples = [load(local / path) for path in row['sample_files']]
        x = np.asarray([sample['embedding']['values'] for sample in samples])
        if row['embedding_signature'] != plan['embedding_signature'] or x.shape != (len(samples), 200):
            raise ValueError('Local embedding identity changed')
        values = {}
        for source in ('pmemo', 'deam', 'emotify-artist', 'jamendo-human-4shards'):
            model = models[source, 'embedding']
            native = head_predictions(model['heads'], x, True)
            for vibe in ('release', 'energize', 'relax', 'uplift'):
                if vibe not in rules[source]:
                    continue
                expr = rules[source][vibe]['expression']
                scores = apply(expr, native, model['target_names'])
                values[source + '__' + vibe] = {'first': float(scores[0]), 'three': float(scores[:3].mean()),
                    'all': float(scores.mean()), 'range': float(np.ptp(scores)),
                    'human_positive': vibe in row['labels']}
        shared = {key: shared_prediction(checkpoint, value.mean(axis=0, keepdims=True))[0].tolist()
                  for key, value in (('first', x[:1]), ('three', x[:3]), ('all', x))}
        records.append({'id': row['id'], 'samples': len(samples), 'prior_inspected': row['prior_inspected'],
                        'labels': row['labels'], 'scores': values, 'checkpoint': shared,
                        'checkpoint_labels': checkpoint['labels']})
    return {'rows': len(rows), 'new_identities': sum(not row['prior_inspected'] for row in rows),
            'positive_counts': {vibe: sum(vibe in row['labels'] for row in rows) for vibe in ('release', 'energize', 'relax', 'uplift')},
            'partial_captures': sum(len(row['sample_files']) < 3 for row in rows),
            'limit': 'Supplementary capture consistency and explicit positives only; unselected vibes, distraction and causal outcomes unknown.',
            'records': records}


def run(directory):
    from . import individual_experiment as experiment
    plan = experiment.validate(directory, execution=True)
    if (directory / 'evaluation.receipt.json').exists():
        raise ValueError('Preserve the completed evaluation')
    for source in experiment.SOURCES:
        check_receipt(directory, 'fit-' + source + '.receipt.json')
    datasets = {name: arrays(directory / f'inputs/{name}.npz') for name in (*experiment.SOURCES, *experiment.PILOTS)}
    metadata = {name: load(directory / f'inputs/{name}.json') for name in datasets}
    rules = load(directory / 'inputs/rules.json')
    models = {(source, family): load(directory / f'models/{source}.{family}.json')
              for source in experiment.SOURCES for family in protocol.FAMILIES}
    native = {source: native_report(directory, source, datasets[source], metadata[source], rules[source]) for source in experiment.SOURCES}
    save(directory / 'native.report.json', native)
    primary, predictions = primary_reports(directory, plan, datasets, metadata, rules, models)
    save(directory / 'primary.report.json', primary)
    np.savez_compressed(directory / 'primary.predictions.npz', **predictions)
    reciprocal, predictions = reciprocal_reports(plan, datasets, metadata, rules, models)
    save(directory / 'reciprocal.report.json', reciprocal)
    np.savez_compressed(directory / 'reciprocal.predictions.npz', **predictions)
    resources, exports = resource_reports(directory, plan, models, rules, datasets)
    save(directory / 'resources.report.json', resources)
    save(directory / 'excerpts.report.json', excerpt_diagnostics(datasets, metadata, rules, models))
    local = local_diagnostics(directory, plan, models, rules)
    save(directory / 'local.report.json', local)
    save(directory / 'evaluation.report.json', {'created_at': stamp(), 'plan_sha256': digest(directory / 'plan.json'),
        'limits': plan['limits'], 'local': {key: value for key, value in local.items() if key != 'records'},
        'decision': 'No automatic adoption; reconcile all declared procedures/domains in the written per-vibe report.',
        'native_sources': len(native), 'primary_cohorts': len(primary), 'reciprocal_tasks': len(reciprocal),
        'research_scorers': len(resources), 'resources_pass': all(value['budget_pass'] for value in resources.values())})
    experiment.validate(directory, execution=True)
    outputs = [directory / name for name in ('native.report.json', 'primary.report.json', 'primary.predictions.npz',
        'reciprocal.report.json', 'reciprocal.predictions.npz', 'resources.report.json', 'excerpts.report.json',
        'local.report.json', 'evaluation.report.json')]
    experiment.receipt(directory, 'evaluation', [*outputs, *exports])
    print(json.dumps({'evaluated': str(directory), 'native_sources': len(native),
                      'primary_cohorts': len(primary), 'reciprocal_tasks': len(reciprocal)}), flush=True)
